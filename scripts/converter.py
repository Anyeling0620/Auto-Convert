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

# ================= 🛡️ 智能配置加载模块 =================
CONFIG_FILE = "config.json"


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return {}


APP_CONFIG = load_config()
SUBJECT = APP_CONFIG.get("subject_name", "通用学科")
DESC = APP_CONFIG.get("description", "")
KEY_INDEX = APP_CONFIG.get("key_index", 0)  # 【核心】获取索引，默认用第一个

INPUT_DIR = "input"
OUTPUT_DIR = "output"
MAX_WORKERS = APP_CONFIG.get("max_workers", 16)

# ================= 🔑 密钥池解析逻辑 =================
# 读取环境变量里的整个字符串
KEY_POOL_STR = os.getenv("ZHIPU_KEY_POOL", "")
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN")


def get_api_key():
    """根据 Config 里的 index 从环境变量池中提取 Key"""
    if not KEY_POOL_STR:
        print("❌ 错误：环境变量 ZHIPU_KEY_POOL 未设置或为空！")
        return None

    # 按逗号切割
    keys = [k.strip() for k in KEY_POOL_STR.split(',') if k.strip()]

    if not keys:
        print("❌ 错误：密钥池中没有有效的 Key！")
        return None

    # 检查索引是否越界
    if KEY_INDEX >= len(keys):
        print(f"⚠️ 警告：config.json 请求第 {KEY_INDEX} 个 Key，但池子里只有 {len(keys)} 个。")
        print(f"🔄 自动回滚使用第 1 个 Key。")
        return keys[0]

    print(f"🔑 已从池中选中第 {KEY_INDEX} 个 Key (Index {KEY_INDEX}) 进行工作。")
    return keys[KEY_INDEX]


# 获取最终的 Key
ZHIPU_API_KEY = get_api_key()

AI_MODEL_NAME = "glm-4-flash"
CHUNK_SIZE = 2000
OVERLAP = 200
MAX_RETRIES = 5
API_TIMEOUT = 120
# =======================================================

if not ZHIPU_API_KEY:
    print("❌ 严重错误：无法获取有效的 ZHIPU_API_KEY，脚本终止。")
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


def normalize_category(raw_cat):
    if not raw_cat: return "综合题"
    cat = raw_cat.strip()
    # 医学
    if "A1" in cat: return "A1型题"
    if "A2" in cat: return "A2型题"
    if "B1" in cat or "配伍" in cat: return "B1型题"
    if "X型" in cat: return "X型题"
    if "病例" in cat or "病案" in cat: return "病例分析题"
    # 通用
    if "多选" in cat or "不定项" in cat: return "多选题"
    if "单选" in cat: return "单选题"
    if "判断" in cat or "是非" in cat: return "判断题"
    if "填空" in cat: return "填空题"
    if "名词" in cat: return "名词解释题"
    if "简答" in cat or "问答" in cat: return "简答题"
    if "论述" in cat: return "论述题"
    # 理工
    if "计算" in cat: return "计算题"
    if "证明" in cat: return "证明题"
    if "编程" in cat or "代码" in cat: return "编程题"
    if "应用" in cat or "设计" in cat: return "应用题"

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
    if not json_str.endswith("]"):
        last_brace = json_str.rfind("}")
        if last_brace != -1:
            json_str = json_str[:last_brace + 1] + "]"
        else:
            return "[]"
    return json_str


def extract_global_answers(full_text):
    print("   🔍 [Step 1] 扫描文档参考答案...")
    safe_text = full_text[:100000]
    prompt = """
    你是一个文档分析师。请提取文档中的“参考答案”。
    要求：只提取答案文本（如 1.A 2.B），纯文本列表。如果不集中，返回“无”。
    """
    try:
        response = client.chat.completions.create(
            model=AI_MODEL_NAME,
            messages=[{"role": "user", "content": prompt + "\n\n" + safe_text}],
            temperature=0.1,
            timeout=120
        )
        return response.choices[0].message.content
    except:
        return ""


def process_single_chunk(args):
    chunk, index, total, answer_key = args

    prompt = f"""
    [系统角色]
    你是一位**{SUBJECT}**领域的试题数据清洗专家。
    背景：{DESC}
    任务：将非结构化文本转换为符合 Schema 的 JSON 数组。

    [输入上下文：参考答案库]
    {answer_key[:5000]}

    [核心处理守则]
    1. **边界丢弃**：切片首尾残缺句子直接丢弃。
    2. **答案匹配**：
       - 优先提取自带答案。
       - 其次查参考答案库。
       - 找不到留空 ""。严禁瞎猜。
    3. **学科归类**：
       - 医学：A1/A2/B1/病例分析。
       - 理工：编程/计算/证明/应用。
       - 通用：单选/多选/填空/判断。

    [JSON 输出结构]
    Strict JSON Array.
    [
      {{
        "category": "String (见映射表)",
        "type": "Enum (SINGLE_CHOICE / MULTI_CHOICE / TRUE_FALSE / FILL_BLANK / ESSAY)",
        "content": "String (题干)",
        "options": [
           {{"label": "A", "text": "..."}}
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
                continue

        except Exception:
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
    print(f"🚀 [{SUBJECT}] 全速工厂启动 | 目标: {target_output_file} | 线程: {MAX_WORKERS}")

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
            # 全速模式：不加 sleep 延迟
            results = list(tqdm(executor.map(process_single_chunk, tasks_args), total=len(chunks), unit="切片"))

            for items in results:
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
        "version": "FullSpeed-V5",
        "subject": SUBJECT,
        "source": "GLM-4-Flash",
        "total_count": len(all_questions),
        "data": all_questions
    }

    with open(target_output_file, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)

    duration = time.time() - start_time
    msg = f"[{SUBJECT}] 转换完成！\n耗时: {duration:.1f}s\n文件: {target_output_file}\n题数: {len(all_questions)}"
    print(f"\n✨ {msg}")

    with open("last_generated_file.txt", "w") as f:
        f.write(target_output_file)


if __name__ == "__main__":
    main()