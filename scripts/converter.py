import json
import os
import uuid
import hashlib
import time
import requests
import random
from docx import Document
from openai import OpenAI
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

# ================= 配置区域 =================
INPUT_DIR = "input"
OUTPUT_DIR = "output"
OUTPUT_FILE = "questions_full.json"

DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN")

AI_BASE_URL = "https://api.deepseek.com"
AI_MODEL_NAME = "deepseek-chat"

# DeepSeek 速率限制较为严格，建议 5-10
MAX_WORKERS = 8
# 切片大小：2000 字符
CHUNK_SIZE = 2000
OVERLAP = 200
MAX_RETRIES = 5

# ===========================================

if not DEEPSEEK_API_KEY:
    print("❌ 严重错误：未找到 DEEPSEEK_API_KEY")
    exit(1)

client = OpenAI(api_key=DEEPSEEK_API_KEY, base_url=AI_BASE_URL)

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
    except:
        pass


def read_docx(file_path):
    if not os.path.exists(file_path): return ""
    try:
        doc = Document(file_path)
        return "\n".join([p.text.strip() for p in doc.paragraphs if p.text.strip()])
    except:
        return ""


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
    json_str = json_str.strip()
    if not json_str.endswith("]"):
        last_brace = json_str.rfind("}")
        if last_brace != -1:
            json_str = json_str[:last_brace + 1] + "]"
    return json_str


def extract_global_answers(full_text):
    """
    【关键修改】读取全文，提取分散的答案
    """
    print("   🔍 [Step 1] DeepSeek 正在全文扫描参考答案 (此过程可能较慢)...")

    # DeepSeek 支持 64K context，这里截取前 100,000 字符 (约5万汉字)，覆盖绝大多数文档
    # 如果文档特别大，DeepSeek 会自动处理或报错，我们做个安全截断
    safe_text = full_text[:100000]

    prompt = """
    你是一个文档分析师。这篇文档采用了“题目与答案交错”的排版方式（例如：50道题 -> 50个答案 -> 50道题...）。

    【任务】
    请通读全文，将分散在文档各个位置的“参考答案”全部提取出来，合并成一个“总答案表”。

    【输出格式】
    请直接输出答案列表，格式为：
    1. A
    2. B
    ...

    不要包含题目内容，只要答案。如果找不到答案，返回"无答案"。
    """

    try:
        response = client.chat.completions.create(
            model=AI_MODEL_NAME,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": safe_text}
            ],
            temperature=0.1,
            stream=False
        )
        ans = response.choices[0].message.content
        print(f"   ✅ 参考答案库构建完成 (长度: {len(ans)} 字符)")
        return ans
    except Exception as e:
        print(f"   ⚠️ 答案提取失败: {e}")
        return ""


def process_single_chunk(args):
    chunk, index, total, answer_key = args

    # 动态裁剪 Answer Key，只保留相关的部分给切片（节省Token）
    # 这里简单处理：如果 Answer Key 很大，只传前 10000 字符。
    # 更优做法是让 DeepSeek 自己在全文里找，但在切片阶段我们只能给它“字典”
    # 对于 DeepSeek，我们可以稍微给多点上下文。

    prompt = f"""
    你是一个试题提取专家。请将文本切片转换为 JSON 数组。

    ### 全局参考答案库 (Global Answer Key)
    --------------------------------------------------
    {answer_key[:15000]} ... (答案库片段)
    --------------------------------------------------

    ### 任务要求
    1. **提取题目**：忽略切片首尾不完整的残缺句。
    2. **配对答案**：
       - 提取题目后，查看其【题号】。
       - 在上方的【全局参考答案库】中查找对应题号的答案。
       - 如果题目文字附近自带答案，优先使用自带答案。
       - **必须填入 answer 字段**。
    3. **推断类型**：自动判断 category 和 type。

    ### JSON 输出格式
    [
      {{
        "category": "单选题",
        "type": "SINGLE_CHOICE", 
        "content": "题干内容...", 
        "options": [{{"label":"A", "text":"..."}}], 
        "answer": "A",
        "analysis": ""
      }}
    ]
    """

    for attempt in range(MAX_RETRIES):
        try:
            response = client.chat.completions.create(
                model=AI_MODEL_NAME,
                messages=[{"role": "system", "content": prompt}, {"role": "user", "content": chunk}],
                temperature=0.0,  # 绝对理智
                max_tokens=4000
            )
            content = response.choices[0].message.content

            if "```json" in content:
                content = content.split("```json")[1].split("```")[0]
            elif "```" in content:
                content = content.split("```")[1].split("```")[0]

            content = content.strip()

            try:
                return json.loads(content)
            except json.JSONDecodeError:
                fixed = repair_json(content)
                return json.loads(fixed)

        except Exception as e:
            wait_time = (2 ** attempt) + random.uniform(0, 1)
            time.sleep(wait_time)

    print(f"❌ Chunk {index + 1} 彻底失败。")
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

    print(f"🚀 DeepSeek-V3 引擎启动 | 并发: {MAX_WORKERS} | 文档数: {len(docx_files)}")

    for filename in docx_files:
        print(f"\n📄 处理文件: {filename}")
        raw_text = read_docx(os.path.join(INPUT_DIR, filename))
        if not raw_text: continue

        # 1. 提取答案 (全文扫描)
        global_answers = extract_global_answers(raw_text)

        # 2. 切片
        chunks = get_chunks(raw_text, CHUNK_SIZE, OVERLAP)

        # 3. 并发处理
        tasks_args = [(chunk, i, len(chunks), global_answers) for i, chunk in enumerate(chunks)]

        chunk_added = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
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
                        chunk_added += 1

        print(f"   ✅ 提取完成: {chunk_added} 道题")

    final_json = {
        "version": "DeepSeek-Interleaved",
        "total_count": len(all_questions),
        "data": all_questions
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)

    duration = time.time() - start_time
    msg = f"DeepSeek 处理完成！\n耗时: {duration:.1f}s\n题目: {len(all_questions)}"
    print(f"\n✨ {msg}")
    send_notification("✅ DeepSeek 题库转换成功", msg.replace('\n', '<br>'))


if __name__ == "__main__":
    main()