import json
import os
import uuid
import hashlib
import time
import requests
import random
import re
from docx import Document
from zhipuai import ZhipuAI
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm  # 进度条库

# ================= 配置区域 =================
INPUT_DIR = "input"
OUTPUT_DIR = "output"
OUTPUT_FILE = "questions_full.json"

ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY")
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN")

AI_MODEL_NAME = "glm-4-flash"

# 【极速模式配置】
MAX_WORKERS = 16       # 并发数拉到 16 (Flash模型QPS很高，完全撑得住)
CHUNK_SIZE = 1500      # 保持 1500 以防止截断
OVERLAP = 200          # 重叠区域
MAX_RETRIES = 5        # 失败重试次数增加到 5 次

# ===========================================

if not ZHIPU_API_KEY:
    print("❌ 严重错误：未找到 ZHIPU_API_KEY")
    exit(1)

client = ZhipuAI(api_key=ZHIPU_API_KEY)

STANDARD_CATEGORIES = {
    "单选题", "多选题", "判断题", "填空题", 
    "名词解释题", "简答题", "论述题", 
    "计算题", "证明题", "应用题", "编程题",
    "配伍题", "案例分析题", "综合题"
}

def send_notification(title, content):
    if not PUSHPLUS_TOKEN: return
    try:
        requests.post("http://www.pushplus.plus/send", json={
            "token": PUSHPLUS_TOKEN, "title": title, "content": content, "template": "html"
        }, timeout=5)
    except: pass

def read_docx(file_path):
    if not os.path.exists(file_path): return ""
    try:
        doc = Document(file_path)
        return "\n".join([p.text.strip() for p in doc.paragraphs if p.text.strip()])
    except: return ""

def get_chunks(text, chunk_size, overlap):
    chunks = []
    start = 0
    total_len = len(text)
    while start < total_len:
        end = min(start + chunk_size, total_len)
        chunks.append(text[start:end])
        if end == total_len: break
        start = end - overlap
    return chunks

def generate_fingerprint(q_obj):
    raw = q_obj.get("content", "") + str(q_obj.get("options", ""))
    return hashlib.md5(raw.encode('utf-8')).hexdigest()

def normalize_category(raw_cat):
    if not raw_cat: return "综合题"
    cat = raw_cat.strip()
    
    if "多选" in cat or "不定项" in cat: return "多选题"
    if "单选" in cat or "A1" in cat or "A2" in cat: return "单选题"
    if "判断" in cat or "是非" in cat: return "判断题"
    if "填空" in cat: return "填空题"
    if "配伍" in cat or "连线" in cat or "B1" in cat: return "配伍题"
    if "名词" in cat: return "名词解释题"
    if "论述" in cat: return "论述题"
    if "简答" in cat or "问答" in cat: return "简答题"
    if "计算" in cat: return "计算题"
    if "编程" in cat or "代码" in cat: return "编程题"
    if "应用" in cat: return "应用题"
    if "证明" in cat: return "证明题"
    if "案例" in cat or "病例" in cat: return "案例分析题"
    
    if cat in STANDARD_CATEGORIES: return cat
    if not cat.endswith("题"): return cat + "题"
    return cat

def repair_json(json_str):
    """JSON 修复手术"""
    json_str = json_str.strip()
    if not json_str.endswith("]"):
        last_brace = json_str.rfind("}")
        if last_brace != -1:
            json_str = json_str[:last_brace+1] + "]"
    return json_str

def extract_global_answers(full_text):
    print("   🔍 [Step 1] 扫描全局参考答案...")
    prompt = "请提取文档中的“参考答案”部分。如果找不到，返回'无'。"
    try:
        response = client.chat.completions.create(
            model=AI_MODEL_NAME,
            messages=[{"role": "user", "content": prompt + "\n\n" + full_text[:80000]}],
            temperature=0.01,
            top_p=0.1,
            max_tokens=4000
        )
        return response.choices[0].message.content
    except: return ""

def process_single_chunk(args):
    """
    单个切片处理逻辑
    args: (chunk, index, total_chunks, answer_key)
    """
    chunk, index, total, answer_key = args

    # 建议使用 f-string 进行拼接
    prompt = f"""
    ### Role & Objective
    你是一个专业的试题数据结构化提取引擎。你的任务是读取非结构化的文本切片，将其转换为严格的 JSON 数组。
    你的输出将被代码直接解析，因此严禁输出任何 Markdown 标记（如 ```json）、开场白或结束语。

    ### Context: Reference Answer Key
    以下是本文档的全局参考答案（仅供匹配使用）。
    当你在题目文本中找不到答案时，请根据【题号】或【题目内容摘要】在此库中查找。
    --------------------------------------------------
    {answer_key[:2000]} ... (答案库片段)
    --------------------------------------------------

    ### Processing Rules (Strict Execution)

    1. **边界截断处理 (Anti-Truncation)**:
       - 输入文本是文档的一个切片（Chunk）。
       - **核心规则**：如果切片开头的第一题不完整（只有选项无题干），或者切片末尾的最后一题不完整（只有题干无选项），**直接丢弃**。只提取中间完整的题目。

    2. **答案匹配逻辑 (Answer Matching)**:
       - **优先级 1**：题目自带答案（例如题干括号内、题干末尾、选项下方标注的“【答案】”）。
       - **优先级 2**：如果在文本中找不到，请去上面的【Reference Answer Key】中查找对应题号的答案。
       - **优先级 3**：如果都找不到，`answer` 字段留空字符串。

    3. **题型标准化 (Category Normalization)**:
       - 根据题目特征（是否有选项、选项数量、是否有“多选”字样）自动推断 `category` 和 `type`。
       - **单选题** (SINGLE_CHOICE): 有 A,B,C,D 选项，且答案只有一个。
       - **多选题** (MULTI_CHOICE): 有选项，且答案包含多个字母，或题干标明“多选/不定项”。
       - **判断题** (TRUE_FALSE): 选项为 对/错、T/F、是/否。
       - **填空题** (FILL_BLANK): 题干中有下划线 `_` 或括号，且无选项。
       - **简答/计算/编程** (ESSAY): 无选项，需要文字回答。

    4. **数据清洗**:
       - 移除题干开头的题号（如 "1. " 或 "(1)"），将其放入 `number` 字段（如果无法提取则由代码生成）。
       - 移除选项开头的标识符（如 "A."），将其放入 `label` 字段。

    ### Output Schema (JSON Array)
    请输出一个 JSON 数组，数组中每个对象必须包含以下字段：

    [
      {{
        "category": "单选题",          // 标准化分类：单选题/多选题/判断题/填空题/简答题/名词解释题/计算题
        "type": "SINGLE_CHOICE",      // 枚举：SINGLE_CHOICE / MULTI_CHOICE / TRUE_FALSE / FILL_BLANK / ESSAY
        "content": "题干文本...",      // 必须清洗掉开头的题号
        "options": [                  // 选择题必填，非选择题为空数组 []
          {{"label": "A", "text": "选项内容"}},
          {{"label": "B", "text": "选项内容"}}
        ],
        "answer": "A",                // 如果是多选则为 "ABC"，判断题为 "正确/错误"
        "analysis": "解析内容"         // 如果文本中有【解析】，请提取；否则留空
      }}
    ]
    """
    
    # === 智能重试机制 (Exponential Backoff) ===
    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            # 动态调整温度：如果重试，稍微增加一点温度避免死循环
            temp = 0.1 if attempt == 0 else 0.3
            
            response = client.chat.completions.create(
                model=AI_MODEL_NAME,
                messages=[{"role": "system", "content": prompt}, {"role": "user", "content": chunk}],
                temperature=0.01,
                top_p=0.1,
                max_tokens=4000
            )
            content = response.choices[0].message.content
            
            # 清洗
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]
            
            content = content.strip()
            
            try:
                return json.loads(content)
            except json.JSONDecodeError:
                # 尝试修复
                fixed = repair_json(content)
                return json.loads(fixed)
                
        except Exception as e:
            last_error = e
            # 指数退避：第一次等1s，第二次2s，第三次4s... 加上随机抖动
            wait_time = (2 ** attempt) + random.uniform(0, 1)
            # print(f"⚠️ Chunk {index+1} 失败，{wait_time:.1f}s 后重试... ({e})")
            time.sleep(wait_time)
    
    # 如果重试 5 次都失败
    print(f"❌ Chunk {index+1} 彻底失败，已跳过。错误: {last_error}")
    return []

def main():
    start_time = time.time()
    
    if not os.path.exists(INPUT_DIR): os.makedirs(INPUT_DIR)
    docx_files = [f for f in os.listdir(INPUT_DIR) if f.endswith(".docx")]
    
    if not docx_files:
        print("❌ input 目录为空。")
        return
    
    all_questions = []
    seen_hashes = set()
    
    print(f"🚀 极速模式启动 | 并发: {MAX_WORKERS} | 文档数: {len(docx_files)}")

    for filename in docx_files:
        print(f"\n📄 处理文件: {filename}")
        raw_text = read_docx(os.path.join(INPUT_DIR, filename))
        if not raw_text: continue

        # 1. 提取答案
        global_answers = extract_global_answers(raw_text)

        # 2. 切片
        chunks = get_chunks(raw_text, CHUNK_SIZE, OVERLAP)
        
        # 3. 并发处理 (带进度条)
        # 准备参数
        tasks_args = [(chunk, i, len(chunks), global_answers) for i, chunk in enumerate(chunks)]
        
        chunk_added_count = 0
        
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 使用 tqdm 显示进度条
            results = list(tqdm(executor.map(process_single_chunk, tasks_args), total=len(chunks), unit="切片"))
            
            for items in results:
                if items:
                    for item in items:
                        fp = generate_fingerprint(item)
                        if fp in seen_hashes: continue
                        seen_hashes.add(fp)
                        
                        item['category'] = normalize_category(item.get('category', '综合题'))
                        item['id'] = str(uuid.uuid4())
                        item['number'] = len(all_questions) + 1
                        item['chapter'] = filename.replace(".docx", "")
                        all_questions.append(item)
                        chunk_added_count += 1
                        
        print(f"   ✅ 提取完成: {chunk_added_count} 道题")

    # 保存
    final_json = {
        "version": "Turbo-V7",
        "total_count": len(all_questions),
        "data": all_questions
    }
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)

    duration = time.time() - start_time
    msg = f"处理完成！\n耗时: {duration:.1f}s\n总题数: {len(all_questions)}\n并发: {MAX_WORKERS}"
    print(f"\n✨ {msg}")
    send_notification("✅ 题库转换(极速版)", msg.replace('\n', '<br>'))

if __name__ == "__main__":
    main()