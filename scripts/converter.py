import json
import os
import uuid
import hashlib
import time
import requests
import random
import threading
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

# 1. 替换 Base URL (这是硅基流动的 API 地址)
AI_BASE_URL = "https://api.siliconflow.cn/v1"

# 2. 替换模型名称 (注意：硅基流动的模型名通常带有 deepseek-ai 前缀)
# 具体名称请去硅基流动后台确认，通常是 "deepseek-ai/DeepSeek-V3"
AI_MODEL_NAME = "deepseek-ai/DeepSeek-V3"

API_TIMEOUT = 120  # 设置超时时间为 120 秒

# 【稳定模式配置】
# 并发数：降回 8，保证不撞墙
MAX_WORKERS = 8
CHUNK_SIZE = 2000
OVERLAP = 200
MAX_RETRIES = 5
# 发射间隔：每 0.5 秒发射一个请求，平滑流量
REQUEST_INTERVAL = 0.5

# 全局冷却锁：当遇到 429 时，所有线程暂缓发送
GLOBAL_COOLDOWN_EVENT = threading.Event()
GLOBAL_COOLDOWN_EVENT.set()  # 初始状态为绿灯

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
    if "```json" in json_str:
        json_str = json_str.split("```json")[1].split("```")[0]
    elif "```" in json_str:
        json_str = json_str.split("```")[1].split("```")[0]
    json_str = json_str.strip()
    if not json_str.endswith("]"):
        last_brace = json_str.rfind("}")
        if last_brace != -1:
            json_str = json_str[:last_brace + 1] + "]"
    return json_str


def extract_global_answers(full_text):
    print("   🔍 [Step 1] DeepSeek 正在全文扫描参考答案...")
    # 安全截取，防止超长
    safe_text = full_text[:100000]
    prompt = """
    你是一个文档分析师。请提取文档中的“参考答案”部分。
    要求：只提取答案文本（如 1.A 2.B），合并成一个列表。
    """
    try:
        response = client.chat.completions.create(
            model=AI_MODEL_NAME,
            messages=[{"role": "system", "content": prompt}, {"role": "user", "content": safe_text}],
            temperature=0.1,
            timeout=120
        )
        ans = response.choices[0].message.content
        print(f"   ✅ 参考答案库构建完成 (长度: {len(ans)} 字符)")
        return ans
    except Exception as e:
        print(f"   ⚠️ 答案提取失败: {e}")
        return ""


def trigger_global_cooldown():
    """触发全局冷却：如果有一个线程被限流，大家一起停一会"""
    if GLOBAL_COOLDOWN_EVENT.is_set():
        # print("   ❄️ 检测到限流，全局暂停 5 秒...")
        GLOBAL_COOLDOWN_EVENT.clear()  # 红灯
        time.sleep(5)
        GLOBAL_COOLDOWN_EVENT.set()  # 绿灯


def process_single_chunk(args):
    chunk, index, total, answer_key = args

    # ==================================================================================
    # ⚡ 终极严谨版 Prompt (中文工业级)
    # ==================================================================================
    prompt = f"""
    [系统角色设定]
    你是一个严格的“试题数据结构化提取引擎”。你**不是**聊天助手。
    你的唯一任务是将输入的文本切片解析为合法的 JSON 数组。

    [全局上下文：参考答案库]
    -----------------------------------------------------------------------
    {answer_key[:15000]} ... (若过长已截断)
    -----------------------------------------------------------------------

    [严格执行守则]

    1. **边界截断处理 (至关重要)**
       - 输入文本是长文档的一个切片。
       - **直接丢弃**切片开头或结尾处不完整的残缺句子（例如只有选项没有题干，或只有题干没有选项）。
       - 只提取中间完整的题目。

    2. **答案匹配逻辑 (优先级顺序)**
       - **优先级 1 (自带答案)**：优先提取题目文本中自带的答案（例如括号内的答案、题干末尾的答案、选项下方的“【答案】”）。
       - **优先级 2 (查全局库)**：提取【题号】（如 "53."），去上方的 [全局上下文：参考答案库] 中查找对应答案。
       - **优先级 3 (留空)**：如果以上两者都找不到，`answer` 字段必须留空字符串 ""。**严禁瞎猜。**

    3. **数据清洗规则**
       - **内容清洗**：移除题干开头的题号（例如将 "1. 什么是..." 清洗为 "什么是..."）。
       - **选项清洗**：移除选项开头的标签（例如将 "A. 苹果" 清洗为 "苹果"），标签放入 `label` 字段。
       - **类型推断 (Type Inference)**：
         - 4个选项 + 1个答案 = "SINGLE_CHOICE"
         - 选项是 对/错 或 T/F = "TRUE_FALSE"
         - 多个答案 (如 "ABC") 或包含关键字 "多选/不定项" = "MULTI_CHOICE"
         - 无选项 + 下划线 "_" 或 "()" = "FILL_BLANK"
         - 无选项 + 问答/简述/代码/计算 = "ESSAY"

    4. **题型归一化 (严格白名单)**
       - `category` 字段只能是以下值之一：
         "单选题", "多选题", "判断题", "填空题", "名词解释题", "简答题", "计算题", "案例分析题", "配伍题", "编程题"。
       - 如果拿不准，归类为 "综合题"。

    [输出格式规范]
    - 输出必须是合法的 JSON Array。
    - **严禁**输出 Markdown 代码块标记（如 ```json）。
    - **严禁**包含任何解释性文字或开场白。
    - 字段 `options` 必须是对象数组：{{"label": "A", "text": "..."}}。

    [待处理文本切片]
    {chunk}
    """
    # ==================================================================================

    for attempt in range(MAX_RETRIES):
        try:
            # 动态温度控制：初次尝试绝对理性，重试时稍微给点灵活性
            current_temp = 0.0 if attempt < 2 else 0.2

            response = client.chat.completions.create(
                model=AI_MODEL_NAME,
                messages=[{"role": "user", "content": prompt}],
                temperature=current_temp,
                max_tokens=4000,
                timeout=API_TIMEOUT
            )
            content = response.choices[0].message.content

            # 深度清洗：防止 AI 虽然听话但还是忍不住加了 ```json
            content = repair_json(content)

            try:
                parsed_json = json.loads(content)
                if isinstance(parsed_json, list):
                    return parsed_json
                elif isinstance(parsed_json, dict):
                    return [parsed_json]
                else:
                    return []
            except json.JSONDecodeError:
                if attempt == MAX_RETRIES - 1:
                    print(f"      ❌ Chunk {index + 1} JSON 解析彻底失败: {content[:50]}...")
                continue

        except Exception as e:
            # 错误处理保持不变...
            wait_time = (2 ** attempt) + random.uniform(0, 1)
            # 仅在多次重试后打印日志，保持控制台清爽
            if attempt > 2:
                print(f"      ⚠️ Chunk {index + 1} 第 {attempt + 1} 次重试: {e}")
            time.sleep(wait_time)

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

    print(f"🚀 DeepSeek 稳定模式 | 并发: {MAX_WORKERS} | 节流间隔: {REQUEST_INTERVAL}s")

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
                # 【核心】：每发射一颗子弹，停顿一下，防止瞬间击穿 API 限制
                time.sleep(REQUEST_INTERVAL)

            # 使用 tqdm 监控结果
            for future in tqdm(as_completed(futures), total=len(chunks), unit="切片"):
                items = future.result()
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
        "version": "DeepSeek-Stable",
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
    send_notification("✅ 题库转换成功", msg.replace('\n', '<br>'))


if __name__ == "__main__":
    main()