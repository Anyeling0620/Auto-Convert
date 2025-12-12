import json
import os
import uuid
import time
import requests
import random
import re
import datetime
from docx import Document
from zhipuai import ZhipuAI
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

# ================= 🛡️ 配置加载 =================
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
INPUT_DIR = "input"
OUTPUT_DIR = "output"

# ================= 🔑 密钥负载均衡池 (核心升级) =================
KEY_POOL_STR = os.getenv("ZHIPU_KEY_POOL", "")
# 【核心修改】使用正则分割：支持 逗号、换行符、空格 混合分隔
# r'[,\n\s]+' 意味着：只要遇到逗号、换行或空白字符，就切开
if KEY_POOL_STR:
    API_KEYS = [k.strip() for k in re.split(r'[,\n\s]+', KEY_POOL_STR) if k.strip()]
else:
    API_KEYS = []

if not API_KEYS:
    print("❌ 严重错误：ZHIPU_KEY_POOL 为空！请在 GitHub Secrets 中配置。")
    # converter.py 用 exit(1)，validator.py 可以选择 return 或 exit
    # 建议这里保持原脚本的处理逻辑
    if __name__ == "__main__": exit(1)

print(f"🔥 密钥池加载成功：共 {len(API_KEYS)} 个 Key")

print(f"🔥 火力全开模式：已加载 {len(API_KEYS)} 个 API Key 进行负载均衡")


def get_random_client():
    """随机抽取一个 Key 创建客户端"""
    selected_key = random.choice(API_KEYS)
    return ZhipuAI(api_key=selected_key), selected_key[-4:]  # 返回 client 和 key的后4位用于日志


# 并发数策略：Key越多，并发可以开得越大
# 假设每个 Key 能撑住 3-5 个并发，这里动态计算
DYNAMIC_WORKERS = len(API_KEYS) * 6
MAX_WORKERS = APP_CONFIG.get("max_workers", DYNAMIC_WORKERS)
# 限制最大不超过 32 (防止 GitHub Runner 内存爆)
if MAX_WORKERS > 26: MAX_WORKERS = 26

AI_MODEL_NAME = "glm-4-flash"
CHUNK_SIZE = 2000;
OVERLAP = 200;
MAX_RETRIES = 5;
API_TIMEOUT = 40
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN")
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "local")


# ================= 📧 报表推送 =================
def send_report(data):
    if not PUSHPLUS_TOKEN: return
    is_success = data['failed_chunks'] == 0
    color = "#28a745" if is_success else "#dc3545"
    title = "✅ 题库生成成功" if is_success else "⚠️ 生成存在异常"

    html = f"""
    <div style="font-family:sans-serif; max-width:600px; padding:20px; border:1px solid #ddd; border-radius:8px;">
        <div style="border-bottom:2px solid {color}; padding-bottom:10px; margin-bottom:20px;">
            <h2 style="margin:0; color:#333;">{title}</h2>
            <p style="color:#666; font-size:12px; margin:5px 0;">{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
        </div>
        <div style="background:#f8f9fa; padding:10px; border-radius:4px; margin-bottom:15px; font-size:14px;">
            <p style="margin:4px 0;"><b>📚 学科:</b> {SUBJECT}</p>
            <p style="margin:4px 0;"><b>🔑 密钥池:</b> 启用 {len(API_KEYS)} 个 Key</p>
            <p style="margin:4px 0;"><b>🚀 并发:</b> {MAX_WORKERS} 线程</p>
        </div>
        <ul style="padding-left:20px; margin-bottom:20px;">
            <li>⏱️ 耗时: <b>{data['duration']:.1f}s</b></li>
            <li>📄 文件: {data['file_count']} 个</li>
            <li>📝 题目: <b style="color:#007bff; font-size:16px;">{data['total_questions']}</b> 道</li>
            <li>🧩 切片: 成功 {data['success_chunks']} / 失败 <b style="color:red;">{data['failed_chunks']}</b></li>
        </ul>
    """
    if data['errors']:
        html += "<div style='background:#fff3cd; padding:10px; border-radius:4px; border:1px solid #ffeeba;'><h4 style='margin:0 0 10px 0; color:#856404;'>⚠️ 异常详情</h4><ul style='padding-left:20px; color:#856404; font-size:13px;'>"
        for err in data['errors']: html += f"<li style='margin-bottom:4px;'>{err}</li>"
        html += "</ul></div>"
    html += "</div>"
    try:
        requests.post("http://www.pushplus.plus/send",
                      json={"token": PUSHPLUS_TOKEN, "title": f"[{SUBJECT}] 生成报告", "content": html,
                            "template": "html"}, timeout=5)
    except:
        pass


# ================= 🛠️ 核心逻辑 =================
def read_docx(file_path):
    if not os.path.exists(file_path): return ""
    try:
        doc = Document(file_path)
        return "\n".join([p.text.strip() for p in doc.paragraphs if p.text.strip()])
    except:
        return ""


def get_chunks(text, size, overlap):
    chunks = [];
    start = 0;
    total = len(text)
    while start < total:
        end = min(start + size, total)
        chunks.append(text[start:end])
        if end == total: break
        start = end - overlap
    return chunks


def normalize_category(raw):
    if not raw: return "综合题"
    cat = raw.strip()
    if "A1" in cat: return "A1型题"
    if "A2" in cat: return "A2型题"
    if "B1" in cat: return "B1型题"
    if "X型" in cat or "多选" in cat: return "X型题"
    if "单选" in cat: return "单选题"
    if "判断" in cat: return "判断题"
    if "填空" in cat: return "填空题"
    if "简答" in cat: return "简答题"
    if "计算" in cat: return "计算题"
    if "编程" in cat: return "编程题"
    if "病例" in cat: return "病例分析题"
    return cat if cat.endswith("题") else cat + "题"


def repair_json(jstr):
    jstr = jstr.strip()
    if "```json" in jstr:
        jstr = jstr.split("```json")[1].split("```")[0]
    elif "```" in jstr:
        jstr = jstr.split("```")[1].split("```")[0]
    jstr = jstr.strip()
    if not jstr.endswith("]"):
        idx = jstr.rfind("}")
        if idx != -1:
            jstr = jstr[:idx + 1] + "]"
        else:
            return "[]"
    return jstr


def extract_global_answers(txt):
    print("   🔍 扫描参考答案...")
    # 抽取一个 Key 专门用来扫答案
    client, k_id = get_random_client()
    try:
        res = client.chat.completions.create(
            model=AI_MODEL_NAME, messages=[{"role": "user", "content": "提取参考答案，纯文本列表。\n\n" + txt[:100000]}],
            temperature=0.1, timeout=120
        )
        return res.choices[0].message.content
    except:
        return ""


def process_chunk(args):
    chunk, idx, ans_key = args

    prompt = f"""
    [系统角色设定]
    你是由 Python 脚本调用的“全学科试题数据结构化引擎”。
    **你不是聊天助手，严禁输出任何寒暄语、解释性文字或 Markdown 代码标记（如 ```json）。**
    你的唯一任务是将输入的非结构化文本切片，精准解析为符合 Schema 定义的 JSON 数组。

    [当前处理学科]
    - 学科名称：**{SUBJECT}**
    - 学科背景：{DESC}

    [全局上下文：参考答案库]
    {ans_key[:5000]}

    [严格执行守则]
    1. **边界截断处理**：直接丢弃切片首尾不完整段落。
    2. **答案匹配逻辑**：优先自带 > 查表 > 留空。严禁随机生成。
    3. **题型归一化**：医学(A1/B1/病例)，理工(编程/计算)，通用(单选/多选/判断/填空/简答)。

    [输出格式规范 (JSON Schema)]
    必须返回 JSON Array：
    [
      {{
        "category": "String",
        "type": "Enum (SINGLE_CHOICE / MULTI_CHOICE / TRUE_FALSE / FILL_BLANK / ESSAY)",
        "content": "String",
        "options": [{{"label": "A", "text": "..."}}],
        "answer": "String",
        "analysis": "String"
      }}
    ]

    [待处理文本切片]
    {chunk}
    """

    last_err = ""
    for i in range(MAX_RETRIES):
        # 每次重试都换号
        client, k_id = get_random_client()

        try:
            # 缩短后的超时时间
            res = client.chat.completions.create(
                model=AI_MODEL_NAME, messages=[{"role": "user", "content": prompt}],
                temperature=0.1, top_p=0.7, max_tokens=4000, timeout=API_TIMEOUT
            )
            content = repair_json(res.choices[0].message.content)
            try:
                data = json.loads(content)
                if isinstance(data, list): return data, None
                if isinstance(data, dict): return [data], None
                raise ValueError("JSON格式异常")  # 抛出异常进入 except
            except Exception as e:
                # 显式抛出 JSON 错误，触发重试
                raise ValueError(f"JSON解析失败: {str(e)}")

        except Exception as e:

            last_err = str(e)

            # 【核心修改】疯狗模式 (Aggressive Retry)

            # 既然 Key 够多，失败了就别等，直接换个号继续冲

            # 仅保留 0.5 秒的“喘息时间”防止 CPU 空转，而不是等 2s, 4s, 8s

            time.sleep(0.5)

            # 日志还是要打的，方便看是不是真的在换 Key

            if i >= 1:
                tqdm.write(f"   🔄 Chunk {idx + 1} 秒级切换 (Key..{k_id}) -> 重试 {i + 1}")

    return [], f"Chunk {idx + 1} 彻底失败 (API: {last_err})"

def main():
    st = time.time()
    if not os.path.exists(INPUT_DIR): return
    files = [f for f in os.listdir(INPUT_DIR) if f.endswith(".docx")]
    if not files: return

    if not os.path.exists(OUTPUT_DIR): os.makedirs(OUTPUT_DIR)
    exist_files = [f for f in os.listdir(OUTPUT_DIR) if f.startswith("output") and f.endswith(".json")]
    next_idx = 1
    for f in exist_files:
        m = re.search(r'output(\d+)', f)
        if m: next_idx = max(next_idx, int(m.group(1)) + 1)
    target_file = os.path.join(OUTPUT_DIR, f"output{next_idx}.json")

    print(f"🚀 [{SUBJECT}] 全速启动 | Key池: {len(API_KEYS)}个 | 并发: {MAX_WORKERS}")

    all_qs = []
    stats = {"file_count": len(files), "total_chunks": 0, "success_chunks": 0, "failed_chunks": 0, "errors": []}

    for fname in files:
        print(f"\n📄 {fname}")
        txt = read_docx(os.path.join(INPUT_DIR, fname))
        if not txt: continue

        ans = extract_global_answers(txt)
        chunks = get_chunks(txt, CHUNK_SIZE, OVERLAP)
        stats['total_chunks'] += len(chunks)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exc:
            futures = [exc.submit(process_chunk, (c, i, ans)) for i, c in enumerate(chunks)]
            for fut in tqdm(as_completed(futures), total=len(chunks)):
                qs, err = fut.result()
                if err:
                    stats['failed_chunks'] += 1
                    stats['errors'].append(err)
                    print(f"   ❌ {err}")
                else:
                    stats['success_chunks'] += 1
                    if qs:
                        for q in qs:
                            q['id'] = str(uuid.uuid4())
                            q['number'] = len(all_qs) + 1
                            q['chapter'] = fname.replace(".docx", "")
                            q['category'] = normalize_category(q.get('category', '综合题'))
                            if 'analysis' not in q: q['analysis'] = ""
                            all_qs.append(q)

    final = {"version": "MultiKey-V8", "subject": SUBJECT, "data": all_qs}
    with open(target_file, 'w', encoding='utf-8') as f:
        json.dump(final, f, ensure_ascii=False, indent=2)
    with open("last_generated_file.txt", "w") as f:
        f.write(target_file)

    stats['duration'] = time.time() - st
    stats['total_questions'] = len(all_qs)
    print(f"\n✨ 完成！提取 {len(all_qs)} 题")
    send_report(stats)


if __name__ == "__main__": main()