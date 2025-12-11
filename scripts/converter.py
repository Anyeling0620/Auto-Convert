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

# 【狂暴模式配置】
# 并发数：直接拉到 20。如果遇到 429 错误，脚本会自动退避，所以不用怕
MAX_WORKERS = 20
CHUNK_SIZE = 2000
OVERLAP = 200
# 重试：死磕 10 次
MAX_RETRIES = 10
# 超时：给它 180 秒（3分钟），防止因为DeepSeek生成慢而被我们主动断开
API_TIMEOUT = 180

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
    # 移除可能存在的 Markdown 代码块
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
    # 截取头尾，最大化覆盖率
    safe_text = full_text[-50000:] + "\n" + full_text[:20000]
    prompt = """
    你是一个文档分析师。请扫描文档，提取所有“参考答案”部分。
    要求：只提取答案文本（如 1.A 2.B），按顺序排列，合并成一个列表。
    如果找不到，返回“无”。
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


def process_single_chunk(args):
    chunk, index, total, answer_key = args

    prompt = f"""
    你是一个试题提取专家。请将文本切片转换为 JSON 数组。

    ### 全局参考答案库
    {answer_key[:15000]} ... 

    ### 任务
    1. **提取题目**：忽略切片首尾不完整句子。
    2. **配对答案**：根据题号去答案库查找，或使用题目自带答案。**必须填入 answer 字段**。
    3. **推断类型**：自动判断 category 和 type。

    ### JSON 格式
    [
      {{
        "category": "单选题",
        "type": "SINGLE_CHOICE", 
        "content": "题干...", 
        "options": [{{"label":"A", "text":"..."}}], 
        "answer": "A",
        "analysis": ""
      }}
    ]
    """

    last_error = None
    for attempt in range(MAX_RETRIES):
        try:
            # 动态温度：重试次数越多，温度略微升高，防止死循环
            temp = 0.0 if attempt < 2 else 0.2

            response = client.chat.completions.create(
                model=AI_MODEL_NAME,
                messages=[{"role": "system", "content": prompt}, {"role": "user", "content": chunk}],
                temperature=temp,
                max_tokens=4000,
                timeout=API_TIMEOUT  # 设置超时
            )
            content = response.choices[0].message.content

            # 清洗 & 修复
            content = repair_json(content)

            try:
                return json.loads(content)
            except json.JSONDecodeError:
                if attempt == MAX_RETRIES - 1:
                    print(f"      ❌ Chunk {index + 1} JSON 解析失败: {content[:50]}...")
                continue  # 重试

        except Exception as e:
            last_error = e
            # 指数退避：1s, 2s, 4s, 8s...
            wait_time = (2 ** attempt) + random.uniform(0, 1)
            # 只有最后几次重试才打印日志，避免刷屏
            if attempt > 2:
                print(f"      ⚠️ Chunk {index + 1} 重试 ({attempt + 1}/{MAX_RETRIES}): {e}")
            time.sleep(wait_time)

    # 彻底失败，返回 None 以便后续识别
    return None


def main():
    start_time = time.time()

    if not os.path.exists(INPUT_DIR): os.makedirs(INPUT_DIR)
    docx_files = [f for f in os.listdir(INPUT_DIR) if f.endswith(".docx")]

    if not docx_files:
        print("❌ input 目录为空。")
        return

    all_questions = []
    seen_hashes = set()

    print(f"🚀 DeepSeek 狂暴模式 | 并发: {MAX_WORKERS} | 重试: {MAX_RETRIES}次 | 文档: {len(docx_files)}")

    for filename in docx_files:
        print(f"\n📄 处理文件: {filename}")
        raw_text = read_docx(os.path.join(INPUT_DIR, filename))
        if not raw_text: continue

        global_answers = extract_global_answers(raw_text)
        chunks = get_chunks(raw_text, CHUNK_SIZE, OVERLAP)

        # 任务队列
        # 使用字典存储 task: (chunk_data) 方便重试
        tasks_map = {i: (chunks[i], i, len(chunks), global_answers) for i in range(len(chunks))}
        failed_chunks = []

        # === Round 1: 并发处理 ===
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            # 提交任务
            future_to_idx = {
                executor.submit(process_single_chunk, tasks_map[i]): i
                for i in tasks_map
            }

            # 进度条
            for future in tqdm(as_completed(future_to_idx), total=len(chunks), unit="切片"):
                idx = future_to_idx[future]
                result = future.result()

                if result is None:
                    # 记录失败的 Chunk
                    failed_chunks.append(idx)
                else:
                    # 成功处理
                    for item in result:
                        fp = generate_fingerprint(item)
                        if fp in seen_hashes: continue
                        seen_hashes.add(fp)
                        item['category'] = normalize_category(item.get('category', '综合题'))
                        item['id'] = str(uuid.uuid4())
                        item['number'] = len(all_questions) + 1
                        item['chapter'] = filename.replace(".docx", "")
                        all_questions.append(item)

        # === Round 2: 失败切片补救 (串行/低并发慢速重试) ===
        if failed_chunks:
            print(f"\n⚠️ 发现 {len(failed_chunks)} 个失败切片，正在进行慢速补救...")
            for idx in failed_chunks:
                print(f"   🚑 补救 Chunk {idx + 1}...")
                # 补救时给予更高温度，碰运气
                retry_args = tasks_map[idx]
                # 这里我们稍微修改一下重试逻辑，或者直接递归调用 process_single_chunk
                # 简单起见，直接再次调用
                result = process_single_chunk(retry_args)

                if result:
                    print(f"      ✅ 补救成功！")
                    for item in result:
                        fp = generate_fingerprint(item)
                        if fp in seen_hashes: continue
                        seen_hashes.add(fp)
                        item['category'] = normalize_category(item.get('category', '综合题'))
                        item['id'] = str(uuid.uuid4())
                        item['number'] = len(all_questions) + 1
                        item['chapter'] = filename.replace(".docx", "")
                        all_questions.append(item)
                else:
                    print(f"      ❌ 补救失败，放弃该切片。")

    final_json = {
        "version": "DeepSeek-Berserk",
        "total_count": len(all_questions),
        "data": all_questions
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)

    duration = time.time() - start_time
    msg = f"DeepSeek 狂暴处理完成！\n耗时: {duration:.1f}s\n题目: {len(all_questions)}\n并发: {MAX_WORKERS}"
    print(f"\n✨ {msg}")
    send_notification("✅ 题库转换成功", msg.replace('\n', '<br>'))


if __name__ == "__main__":
    main()