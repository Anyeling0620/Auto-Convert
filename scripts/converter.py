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

# ================= 🛡️ 全局配置区域 =================
INPUT_DIR = "input"
OUTPUT_DIR = "output"
# 文件名将自动生成 output{N}.json

ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY")
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN")

AI_MODEL_NAME = "glm-4-flash"
MAX_WORKERS = 16  # 高并发
CHUNK_SIZE = 2000  # 适中切片
OVERLAP = 200  # 必要的重叠防止切断题目
MAX_RETRIES = 5  # 饱和式重试
API_TIMEOUT = 60  # 单次请求超时控制
# =================================================

if not ZHIPU_API_KEY:
    print("❌ 严重错误：未找到 ZHIPU_API_KEY")
    exit(1)

client = ZhipuAI(api_key=ZHIPU_API_KEY)

# 扩展白名单：包含医学、理工、文史
STANDARD_CATEGORIES = {
    "A1型题", "A2型题", "B1型题", "X型题", "配伍题", "病例分析题",
    "单选题", "多选题", "判断题", "填空题",
    "名词解释题", "简答题", "论述题",
    "计算题", "证明题", "编程题", "应用题", "综合题"
}


def get_next_output_filename():
    """🛡️ 自动获取下一个文件名，防止覆盖"""
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
    """🛡️ 鲁棒的文件读取"""
    if not os.path.exists(file_path): return ""
    try:
        doc = Document(file_path)
        # 过滤空行，减少 Token 消耗
        return "\n".join([p.text.strip() for p in doc.paragraphs if p.text.strip()])
    except Exception as e:
        print(f"❌ 读取文件失败 {file_path}: {e}")
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
    """🛡️ 强力归一化：不管 AI 输出什么，强行映射到标准库"""
    if not raw_cat: return "综合题"
    cat = raw_cat.strip()

    # 1. 优先医学术语
    if "A1" in cat: return "A1型题"
    if "A2" in cat: return "A2型题"
    if "B1" in cat or "配伍" in cat: return "B1型题"
    if "X型" in cat: return "X型题"
    if "病例" in cat or "病案" in cat: return "病例分析题"

    # 2. 通用映射
    if "多选" in cat or "不定项" in cat: return "多选题"
    if "单选" in cat: return "单选题"
    if "判断" in cat or "是非" in cat: return "判断题"
    if "填空" in cat: return "填空题"
    if "名词" in cat: return "名词解释题"
    if "简答" in cat or "问答" in cat: return "简答题"
    if "论述" in cat: return "论述题"

    # 3. 理工特色
    if "计算" in cat: return "计算题"
    if "证明" in cat: return "证明题"
    if "编程" in cat or "代码" in cat: return "编程题"
    if "应用" in cat or "设计" in cat: return "应用题"

    if cat in STANDARD_CATEGORIES: return cat
    if not cat.endswith("题"): return cat + "题"
    return cat


def repair_json(json_str):
    """🛡️ JSON 强力修复手术"""
    json_str = json_str.strip()

    # 1. 去除 Markdown 代码块
    if "```json" in json_str:
        json_str = json_str.split("```json")[1].split("```")[0]
    elif "```" in json_str:
        json_str = json_str.split("```")[1].split("```")[0]

    json_str = json_str.strip()

    # 2. 尝试修复截断的数组
    # 如果不是以 ] 结尾，尝试找到最后一个 } 并补上 ]
    if not json_str.endswith("]"):
        last_brace = json_str.rfind("}")
        if last_brace != -1:
            json_str = json_str[:last_brace + 1] + "]"
        else:
            # 极端情况：连一个完整的对象都没有，返回空数组
            return "[]"

    return json_str


def extract_global_answers(full_text):
    print("   🔍 [Step 1] 扫描文档参考答案...")
    # 截取全文扫描 (Flash支持128k context，直接上)
    safe_text = full_text[:100000]
    prompt = """
    你是一个文档分析师。请扫描本文档，提取所有“参考答案”部分。
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
    # ⚡ 严谨级 Prompt (中文版) - 专治 AI 幻觉和格式错误
    # =================================================================
    prompt = f"""
    [系统角色]
    你是一个严格遵循指令的“通用试题数据清洗引擎”。你**不是**聊天机器人。
    你的任务是将非结构化文本转换为符合以下 Schema 的 JSON 数组。

    [输入上下文：参考答案库]
    (当题目中没有自带答案时，请查询此库)
    -----------------------------------
    {answer_key[:5000]}
    -----------------------------------

    [核心处理守则]
    1. **边界丢弃原则**：输入文本是一个切片。如果切片开头的第一句话是不完整的（例如只有选项没有题干），或者切片末尾最后一句话不完整，**必须直接丢弃**。严禁脑补残缺内容。
    2. **答案匹配优先级**：
       - **优先级 1**：题目文本中自带的答案（例如括号内、题干末尾、选项下方的“【答案】”）。
       - **优先级 2**：根据【题号】去上方的 [参考答案库] 中查找。
       - **优先级 3**：如果都找不到，`answer` 字段留空字符串 ""。**严禁随机生成答案。**
    3. **内容清洗**：
       - 移除 `content` 字段开头的题号（如 "1. "）。
       - 移除 `options` 中 `text` 字段开头的标签（如 "A. "），标签放入 `label`。

    [学科题型映射表 (Category Inference)]
    请根据题目内容特征，从下表中选择最准确的分类填入 `category`：
    - **医学类**：
      - 5个选项(A-E)单选 -> "A1型题" 或 "A2型题"
      - 配伍题/共用题干 -> "B1型题"
      - 多选题 -> "X型题"
      - 病例描述 -> "病例分析题"
    - **理工/计算机类**：
      - 代码填空/算法设计 -> "编程题"
      - 数值计算/公式推导 -> "计算题"
      - 证明/推导 -> "证明题"
    - **通用类**：
      - 4个选项单选 -> "单选题"
      - 多个正确答案 -> "多选题"
      - 判断正误(对/错) -> "判断题"
      - 下划线填空 -> "填空题"
      - 无选项问答 -> "简答题"

    [JSON 输出结构 (Strict Schema)]
    必须返回一个 JSON 数组，不要包含 ```json 标记。
    [
      {{
        "category": "String (见映射表)",
        "type": "Enum (SINGLE_CHOICE / MULTI_CHOICE / TRUE_FALSE / FILL_BLANK / ESSAY)",
        "content": "String (清洗后的题干)",
        "options": [
           {{"label": "A", "text": "..."}},
           {{"label": "B", "text": "..."}}
        ],
        "answer": "String (例如 'A', 'ABC', 'True', '代码...')",
        "analysis": ""
      }}
    ]

    [待处理文本]
    {chunk}
    """
    # =================================================================

    for attempt in range(MAX_RETRIES):
        try:
            # 动态温度：重试次数越多，温度略微升高防死循环
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

            # 🛡️ 深度清洗与修复
            content = repair_json(content)

            try:
                res = json.loads(content)
                if isinstance(res, list): return res
                if isinstance(res, dict): return [res]
                # 如果解析出来是空或者其他类型，视为失败
                return []
            except json.JSONDecodeError:
                if attempt == MAX_RETRIES - 1:
                    print(f"      ❌ Chunk {index + 1} JSON 解析彻底失败。")
                continue

        except Exception as e:
            # 指数退避：1s, 2s, 4s...
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

    # 1. 确定输出文件名
    target_output_file = get_next_output_filename()
    print(f"🚀 任务启动 | 将生成文件: {target_output_file} | 线程数: {MAX_WORKERS}")

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
            # 使用 tqdm 包装 executor.map 实现进度条
            results = list(tqdm(executor.map(process_single_chunk, tasks_args), total=len(chunks), unit="切片"))

            for items in results:
                if items:
                    for item in items:
                        # 补全元数据
                        item['id'] = str(uuid.uuid4())
                        item['number'] = len(all_questions) + 1
                        item['chapter'] = filename.replace(".docx", "")
                        item['category'] = normalize_category(item.get('category', '综合题'))
                        if 'analysis' not in item: item['analysis'] = ""

                        all_questions.append(item)
                        chunk_added += 1

        print(f"   ✅ 本文件提取: {chunk_added} 道")

    # 构建最终 JSON
    final_json = {
        "version": "Universal-V2.0",
        "source": "GLM-4-Flash-Auto",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "total_count": len(all_questions),
        "data": all_questions
    }

    with open(target_output_file, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)

    duration = time.time() - start_time
    msg = f"生成完成！\n耗时: {duration:.1f}s\n文件: {target_output_file}\n题数: {len(all_questions)}"
    print(f"\n✨ {msg}")

    # 将文件名写入临时文件，传递给 Validator
    with open("last_generated_file.txt", "w") as f:
        f.write(target_output_file)


if __name__ == "__main__":
    main()