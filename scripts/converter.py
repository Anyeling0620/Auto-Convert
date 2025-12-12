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
from tqdm import tqdm

# ================= 🛡️ 稳健模式配置 =================
INPUT_DIR = "input"
OUTPUT_DIR = "output"

ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY")
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN")

AI_MODEL_NAME = "glm-4-flash"

# 【核心优化配置】
MAX_WORKERS = 5  # 降级：从 16 降为 5，避免拥堵
CHUNK_SIZE = 2000
OVERLAP = 200
MAX_RETRIES = 5
API_TIMEOUT = 60  # 强制超时：60秒不回话就重试，别等20分钟
REQUEST_INTERVAL = 1.0  # 节流阀：每秒只发一个请求，平滑流量
# =================================================

if not ZHIPU_API_KEY:
    print("❌ 严重错误：未找到 ZHIPU_API_KEY")
    exit(1)

client = ZhipuAI(api_key=ZHIPU_API_KEY)

STANDARD_CATEGORIES = {
    "A1型题", "A2型题", "B1型题", "X型题", "配伍题", "病例分析题",
    "单选题", "多选题", "判断题", "填空题",
    "名词解释题", "简答题", "论述题",
    "计算题", "证明题", "编程题", "应用题", "综合题"
}


def get_next_output_filename():
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)

    existing_files = [f for f in os.listdir(OUTPUT_DIR) if f.startswith("output") and f.endswith(".json")]
    max_index = 0
    for f in existing_files:
        match = re.search(r'output(\d+)\.json', f)
        if match:
            idx = int(match.group(1))
            if idx > max_index:
                max_index = idx
    return os.path.join(OUTPUT_DIR, f"output{max_index + 1}.json")


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
    except Exception as e:
        print(f"❌ 读取文件失败: {e}")
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


def normalize_category(raw_cat):
    if not raw_cat: return "综合题"
    cat = raw_cat.strip()

    if "A1" in cat: return "A1型题"
    if "A2" in cat: return "A2型题"
    if "B1" in cat or "配伍" in cat: return "B1型题"
    if "X型" in cat: return "X型题"
    if "病例" in cat or "病案" in cat: return "病例分析题"

    if "多选" in cat or "不定项" in cat: return "多选题"
    if "单选" in cat: return "单选题"
    if "判断" in cat or "是非" in cat: return "判断题"
    if "填空" in cat: return "填空题"
    if "名词" in cat: return "名词解释题"
    if "简答" in cat or "问答" in cat: return "简答题"
    if "论述" in cat: return "论述题"
    if "计算" in cat: return "计算题"
    if "证明" in cat: return "证明题"
    if "编程" in cat or "代码" in cat: return "编程题"
    if "应用" in cat: return "应用题"

    if cat in STANDARD_CATEGORIES: return cat
    if not cat.endswith("题"): return cat + "题"
    return cat


def repair_json(json_str):
    json_str = json_str.strip()
    if "```json" in json_str:
        json_str = json_str.split("```json")[1].split("```")[0]
    elif "```" in json_str:
        json_str = json_str.split("```")[1].split("```")[0]
    json_str = json_str.strip()

    # 尝试自动闭合
    if not json_str.endswith("]"):
        last_brace = json_str.rfind("}")
        if last_brace != -1:
            json_str = json_str[:last_brace + 1] + "]"
        else:
            return "[]"  # 无法修复
    return json_str


def extract_global_answers(full_text):
    print("   🔍 [Step 1] 扫描文档参考答案...")
    safe_text = full_text[:100000]
    prompt = """
    你是一个文档分析师。请提取文档中的“参考答案”部分。
    【要求】
    1. 忽略题目内容，**只提取答案**。
    2. 输出格式为纯文本列表（如：1.A 2.B 3.C ...）。
    3. 如果找不到集中答案，返回“无”。
    """
    try:
        response = client.chat.completions.create(
            model=AI_MODEL_NAME,
            messages=[{"role": "user", "content": prompt + "\n\n" + safe_text}],
            temperature=0.1,
            timeout=120
        )
        return response.choices[0].message.content
    except Exception as e:
        print(f"   ⚠️ 答案扫描失败: {e}")
        return ""


def process_single_chunk(args):
    chunk, index, total, answer_key = args

    # =================================================================
    # ⚡ 严谨级 Prompt (中文版)
    # =================================================================
    prompt = f"""
    [系统角色]
    你是一个严格遵循指令的“通用试题数据清洗引擎”。你**不是**聊天机器人。
    你的任务是将非结构化文本转换为符合 Schema 的 JSON 数组。

    [输入上下文：参考答案库]
    -----------------------------------
    {answer_key[:5000]}
    -----------------------------------

    [核心处理守则]
    1. **边界丢弃原则**：输入文本是一个切片。如果切片开头的第一句话是不完整的，或者切片末尾最后一句话不完整，**必须直接丢弃**。
    2. **答案匹配优先级**：
       - 优先：题目文本中自带的答案。
       - 其次：根据【题号】去参考答案库查找。
       - 最后：如果都找不到，`answer` 字段留空 ""。**严禁随机生成答案。**
    3. **内容清洗**：
       - 移除题干开头的题号。
       - 移除选项开头的标签 A. B. 等。

    [JSON 输出结构 (Strict Schema)]
    必须返回一个 JSON 数组。
    [
      {{
        "category": "String (如 A1型题, 单选题, 填空题...)",
        "type": "Enum (SINGLE_CHOICE / MULTI_CHOICE / TRUE_FALSE / FILL_BLANK / ESSAY)",
        "content": "String (清洗后的题干)",
        "options": [
           {{"label": "A", "text": "..."}},
           {{"label": "B", "text": "..."}}
        ],
        "answer": "String",
        "analysis": ""
      }}
    ]

    [待处理文本]
    {chunk}
    """

    for attempt in range(MAX_RETRIES):
        try:
            temp = 0.0 if attempt < 2 else 0.1

            response = client.chat.completions.create(
                model=AI_MODEL_NAME,
                messages=[{"role": "user", "content": prompt}],
                temperature=temp,
                top_p=0.7,
                max_tokens=4000,
                timeout=API_TIMEOUT
            )
            content = response.choices[0].message.content

            content = repair_json(content)

            try:
                res = json.loads(content)
                if isinstance(res, list): return res
                if isinstance(res, dict): return [res]
                return []
            except json.JSONDecodeError:
                if attempt == MAX_RETRIES - 1:
                    print(f"      ❌ Chunk {index + 1} JSON 解析彻底失败。")
                continue

        except Exception as e:
            # 只有超时才打印简单日志
            # print(f"Wait {index}")
            wait_time = (2 ** attempt) + random.uniform(0, 1)
            time.sleep(wait_time)

    return []


def main():
    start_time = time.time()

    if not os.path.exists(INPUT_DIR): os.makedirs(INPUT_DIR)
    docx_files = [f for f in os.listdir(INPUT_DIR) if f.endswith(".docx")]

    if not docx_files:
        print("❌ input 目录为空。")
        return

    target_output_file = get_next_output_filename()
    print(f"🚀 稳健模式启动 | 目标: {target_output_file} | 线程: {MAX_WORKERS}")

    all_questions = []

    for filename in docx_files:
        print(f"\n📄 处理文件: {filename}")
        raw_text = read_docx(os.path.join(INPUT_DIR, filename))
        if not raw_text: continue

        global_answers = extract_global_answers(raw_text)
        chunks = get_chunks(raw_text, CHUNK_SIZE, OVERLAP)

        tasks_args = [(chunk, i, len(chunks), global_answers) for i, chunk in enumerate(chunks)]
        chunk_added = 0

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 手动提交任务，控制发射频率
            futures = []
            for arg in tasks_args:
                futures.append(executor.submit(process_single_chunk, arg))
                # 【关键】每发射一颗子弹，停顿 1 秒，防止拥堵
                time.sleep(REQUEST_INTERVAL)

            # 使用 tqdm 监控
            for future in tqdm(as_completed(futures), total=len(chunks), unit="切片"):
                items = future.result()
                if items:
                    for item in items:
                        item['id'] = str(uuid.uuid4())
                        item['number'] = len(all_questions) + 1
                        item['chapter'] = filename.replace(".docx", "")
                        item['category'] = normalize_category(item.get('category', '综合题'))
                        if 'analysis' not in item: item['analysis'] = ""
                        all_questions.append(item)
                        chunk_added += 1

        print(f"   ✅ 本文件提取: {chunk_added} 道")

    final_json = {
        "version": "Universal-Stable-V3",
        "source": "GLM-4-Flash",
        "total_count": len(all_questions),
        "data": all_questions
    }

    with open(target_output_file, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)

    duration = time.time() - start_time
    msg = f"生成完成！\n耗时: {duration:.1f}s\n文件: {target_output_file}\n题数: {len(all_questions)}"
    print(f"\n✨ {msg}")

    with open("last_generated_file.txt", "w") as f:
        f.write(target_output_file)


if __name__ == "__main__":
    main()