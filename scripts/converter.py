import json
import os
import uuid
import hashlib
import time
import re
import requests
from docx import Document
from zhipuai import ZhipuAI
from concurrent.futures import ThreadPoolExecutor, as_completed

# ================= 配置区域 =================
INPUT_DIR = "input"
OUTPUT_DIR = "output"
OUTPUT_FILE = "questions_full.json"

# 从环境变量获取 Key
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY")
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN")

# 【核心调优参数】
AI_MODEL_NAME = "glm-4-flash"  # 极速版：高并发、低延迟、长上下文
MAX_WORKERS = 8                # 并发线程数：Flash模型支持较高并发，8-10是安全区
CHUNK_SIZE = 4000              # 切片大小：4000字符，保证上下文完整
OVERLAP = 500                  # 重叠区域：防止题目被切断
AI_TEMPERATURE = 0.01          # 温度极低：强制AI“死板”一点，保证JSON格式正确

# ===========================================

# 初始化客户端
if not ZHIPU_API_KEY:
    print("❌ 严重错误：未找到 ZHIPU_API_KEY，请检查 GitHub Secrets 或本地环境变量！")
    exit(1)

client = ZhipuAI(api_key=ZHIPU_API_KEY)

# 标准分类白名单
STANDARD_CATEGORIES = {
    "单选题", "多选题", "判断题", "填空题", "简答题", 
    "名词解释题", "案例分析题", "计算题", "证明题", "配伍题"
}

def send_notification(title, content):
    """发送微信通知 (PushPlus)"""
    if not PUSHPLUS_TOKEN:
        print("⚠️ 未配置 PUSHPLUS_TOKEN，跳过微信通知。")
        return
    
    url = "http://www.pushplus.plus/send"
    data = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content,
        "template": "html"
    }
    try:
        resp = requests.post(url, json=data, timeout=10)
        if resp.status_code == 200:
            print("✅ 微信通知已发送！")
        else:
            print(f"⚠️ 微信通知发送失败: {resp.text}")
    except Exception as e:
        print(f"⚠️ 微信通知网络错误: {e}")

def read_docx(file_path):
    """鲁棒的 Docx 读取"""
    if not os.path.exists(file_path): return ""
    try:
        doc = Document(file_path)
        # 过滤空行，合并段落
        return "\n".join([p.text.strip() for p in doc.paragraphs if p.text.strip()])
    except Exception as e:
        print(f"❌ 无法读取文件 {file_path}: {e}")
        return ""

def get_chunks(text, chunk_size, overlap):
    """滑动窗口切分"""
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
    """生成指纹用于去重 (内容+选项)"""
    raw = q_obj.get("content", "") + str(q_obj.get("options", ""))
    return hashlib.md5(raw.encode('utf-8')).hexdigest()

def normalize_category(raw_cat):
    """强力归一化分类名称"""
    if not raw_cat: return "综合题"
    cat = raw_cat.strip()
    
    # 关键词映射
    if "多选" in cat or "不定项" in cat: return "多选题"
    if "单选" in cat: return "单选题"
    if "判断" in cat or "是非" in cat: return "判断题"
    if "填空" in cat: return "填空题"
    if "名词" in cat: return "名词解释题"
    if "简答" in cat or "问答" in cat or "论述" in cat: return "简答题"
    if "计算" in cat: return "计算题"
    if "证明" in cat: return "证明题"
    if "案例" in cat or "病例" in cat: return "案例分析题"
    if "配伍" in cat or "连线" in cat: return "配伍题"

    # 白名单直通
    if cat in STANDARD_CATEGORIES: return cat
    
    # 兜底：强制加“题”字
    if not cat.endswith("题"): return cat + "题"
    return cat

def extract_global_answers(full_text):
    """第一步：提取全局答案 (利用 Flash 的长上下文)"""
    print("   🔍 [Step 1] 正在全文档扫描提取参考答案...")
    prompt = """
    你是一个文档分析助手。请阅读下面的文档全文，提取出其中的“参考答案”部分。
    
    【要求】
    1. 寻找文档中集中的“答案页”、“Key”、“参考答案”部分。
    2. 如果答案分散在题目后，也请尽力提取。
    3. 如果完全找不到答案，返回"无答案"。
    4. **只返回答案文本**，不要包含题目内容，不要废话。
    """
    
    try:
        # 截取前 80k 字符 (Flash 支持 128k，留余量给 System Prompt)
        safe_text = full_text[:80000] 
        response = client.chat.completions.create(
            model=AI_MODEL_NAME,
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": safe_text}
            ],
            temperature=0.1
        )
        answers = response.choices[0].message.content
        print(f"   ✅ 参考答案提取完毕 (长度: {len(answers)} 字符)")
        return answers
    except Exception as e:
        print(f"   ⚠️ 提取答案失败 (可能是文档过大或 API 错误): {e}")
        return ""

def clean_json_string(content):
    """清洗 AI 返回的字符串，提取 JSON 部分"""
    try:
        # 1. 尝试去除 Markdown 代码块
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]
        
        # 2. 尝试寻找最外层的 []
        start = content.find('[')
        end = content.rfind(']')
        if start != -1 and end != -1:
            content = content[start:end+1]
            
        return content.strip()
    except Exception:
        return content

def process_single_chunk(chunk_data):
    """[Step 2] 并发处理单个切片"""
    chunk, index, total, answer_key = chunk_data
    
    prompt = f"""
    你是一个通用试题提取助手。请将输入的文本片段转换为严格的 JSON 数组。
    
    【参考答案库 (用于自动填空)】
    ----------------
    {answer_key[:5000]}
    ----------------
    
    【任务要求】
    1. **识别题目**：从文本中提取完整的题目。
    2. **忽略残缺**：切片开头和结尾如果不完整，直接丢弃。
    3. **匹配答案**：根据题号或内容，从上面的参考答案库中找到对应的答案填入 `answer` 字段。如果找不到，留空。
    4. **推断类型**：自动判断 `category` (如 单选题, 判断题) 和 `type`。

    【输出格式 (Strict JSON)】
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
    
    try:
        response = client.chat.completions.create(
            model=AI_MODEL_NAME,
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": chunk}],
            temperature=AI_TEMPERATURE, # 0.01 保证格式稳定
            top_p=0.7,
            max_tokens=4000 # 允许长输出
        )
        raw_content = response.choices[0].message.content
        clean_content = clean_json_string(raw_content)
        
        return json.loads(clean_content)
        
    except json.JSONDecodeError:
        # 常见错误：AI 没说完被截断，或者输出了非法 JSON
        print(f"      ⚠️ Chunk {index+1}: JSON 解析失败 (可能是内容被截断或格式错误)")
        return []
    except Exception as e:
        print(f"      ⚠️ Chunk {index+1}: API 调用错误: {e}")
        return []

def main():
    start_time = time.time()
    
    # 检查输入目录
    if not os.path.exists(INPUT_DIR): os.makedirs(INPUT_DIR)
    docx_files = [f for f in os.listdir(INPUT_DIR) if f.endswith(".docx")]
    
    if not docx_files:
        print("❌ input 目录中没有找到 .docx 文件。")
        return
    
    all_questions = []
    seen_hashes = set()
    total_files = len(docx_files)
    
    print(f"🚀 启动任务：发现 {total_files} 个文档，使用模型 {AI_MODEL_NAME}，并发数 {MAX_WORKERS}")

    for file_idx, filename in enumerate(docx_files):
        print(f"\n📄 [{file_idx+1}/{total_files}] 处理文件: {filename}")
        file_path = os.path.join(INPUT_DIR, filename)
        
        # 读取
        raw_text = read_docx(file_path)
        if not raw_text: continue

        # 1. 提取答案 (串行)
        global_answers = extract_global_answers(raw_text)

        # 2. 切片
        chunks = get_chunks(raw_text, CHUNK_SIZE, OVERLAP)
        print(f"   📂 切分为 {len(chunks)} 个片段，开始 {MAX_WORKERS} 线程并发处理...")
        
        # 3. 并发提取 (并行)
        file_added_count = 0
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 提交所有任务
            futures = [executor.submit(process_single_chunk, (chunk, i, len(chunks), global_answers)) 
                       for i, chunk in enumerate(chunks)]
            
            # 处理结果
            for future in as_completed(futures):
                items = future.result()
                if items:
                    for item in items:
                        # 去重
                        fp = generate_fingerprint(item)
                        if fp in seen_hashes: continue
                        seen_hashes.add(fp)
                        
                        # 标准化 & 补全
                        item['category'] = normalize_category(item.get('category', '综合题'))
                        item['id'] = str(uuid.uuid4())
                        item['number'] = len(all_questions) + 1
                        item['chapter'] = filename.replace(".docx", "")
                        
                        all_questions.append(item)
                        file_added_count += 1
                        
        print(f"   ✅ 文件处理完成，提取有效题目: {file_added_count} 道")

    # 保存结果
    final_json = {
        "version": "Universal-HighConcurrency",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_count": len(all_questions),
        "data": all_questions
    }
    
    if not os.path.exists(OUTPUT_DIR): os.makedirs(OUTPUT_DIR)
    out_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)

    # 统计与通知
    duration = time.time() - start_time
    msg = (
        f"<b>任务完成报告</b><br>"
        f"耗时: {duration:.1f} 秒<br>"
        f"处理文档: {total_files} 个<br>"
        f"提取题目: {len(all_questions)} 道<br>"
        f"并发线程: {MAX_WORKERS}<br>"
        f"模型: {AI_MODEL_NAME}"
    )
    print(f"\n✨ {msg.replace('<br>', '\n')}")
    print(f"💾 结果已保存至: {out_path}")
    
    send_notification("✅ 题库转换成功", msg)

if __name__ == "__main__":
    main()