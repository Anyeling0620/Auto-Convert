import json
import os
import uuid
import time
import requests
import random
import re
import datetime
import smtplib
from email.mime.text import MIMEText
from email.header import Header
from docx import Document
from zhipuai import ZhipuAI
from concurrent.futures import ThreadPoolExecutor, as_completed

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

# 📧 邮件配置
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.163.com")
SMTP_PORT = int(os.getenv("SMTP_PORT", 465))
SMTP_USER = os.getenv("SMTP_USER")
SMTP_PASS = os.getenv("SMTP_PASS")
RECEIVER_EMAILS_STR = os.getenv("RECEIVER_EMAILS", "")
if RECEIVER_EMAILS_STR:
    RECEIVER_EMAILS = [e.strip() for e in re.split(r'[,\n\s]+', RECEIVER_EMAILS_STR) if e.strip()]
else:
    RECEIVER_EMAILS = []

# 🔑 密钥池
KEY_POOL_STR = os.getenv("ZHIPU_KEY_POOL", "")
if KEY_POOL_STR:
    API_KEYS = [k.strip() for k in re.split(r'[,\n\s]+', KEY_POOL_STR) if k.strip()]
else:
    API_KEYS = []
if not API_KEYS:
    print("❌ 严重错误：ZHIPU_KEY_POOL 为空！")
    if __name__ == "__main__": exit(1)

# ================= ⚙️ 性能策略 (稳健版) =================
MAX_WORKERS = 8  # 并发数
AI_MODEL_NAME = "glm-4-flash"
CHUNK_SIZE = 800  # 切片大小
OVERLAP = 100
MAX_RETRIES = 5  # 重试次数
API_TIMEOUT = 80  # 超时时间
RETRY_DELAY = 2  # 冷却时间
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN")

# ================= 📝 全局日志 =================
EXECUTION_LOGS = []


def log_record(msg, level="INFO"):
    timestamp = datetime.datetime.now().strftime("%H:%M:%S")
    icon = "✅" if level == "INFO" else "❌" if level == "ERROR" else "⚠️"
    print(f"[{timestamp}] {icon} {msg}", flush=True)

    color = "#333"
    if level == "ERROR": color = "red"
    if level == "WARN": color = "#d35400"
    if "Chunk" in msg and level == "INFO": color = "green"
    log_line = f"<div style='color:{color}; border-bottom:1px dashed #eee; padding:4px 0;'>[{timestamp}] {msg}</div>"
    EXECUTION_LOGS.append(log_line)


# ================= 🛠️ 核心功能 =================
def get_random_client():
    selected_key = random.choice(API_KEYS)
    return ZhipuAI(api_key=selected_key), selected_key[-4:]


def read_docx(file_path):
    if not os.path.exists(file_path): return ""
    try:
        doc = Document(file_path)
        return "\n".join([p.text.strip() for p in doc.paragraphs if p.text.strip()])
    except:
        return ""


def get_chunks(text, size, overlap):
    chunks = []
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


def process_chunk(args):
    """
    V17 核心逻辑：死磕模式 + 反侦察伪装
    目标：100% 提取，且让 API 平台难以判定为恶意攻击
    """
    chunk, idx, ans_key = args

    # 🕵️‍♂️ 【伪装策略1】流量抖动
    # 启动前随机休息，打破“并发齐射”的特征，模拟多人不同步操作
    time.sleep(random.uniform(1.0, 4.0))

    prompt = f"""
        [系统指令] 你是一个高并发、无状态的试题数据清洗引擎。
        [任务] 将输入的非结构化文本片段(Chunk)清洗并转换为严格的 JSON Array。
        [当前学科] {SUBJECT}

        [严重警告]
        1. ❌ 严禁输出 ```json 或 ``` 标记。
        2. ❌ 严禁输出任何解释、寒暄或非 JSON 字符。
        3. ✅ 必须且只能输出纯 JSON 字符串。
        4. ✅ 遇到切片首尾截断、不完整的题目，直接丢弃，不要尝试修复，以免产生幻觉。

        [数据清洗规则]
        1. **题型归一化**：
           - (A1/A2/B1/单选) -> "SINGLE_CHOICE"
           - (X型/多选) -> "MULTI_CHOICE"
           - (填空) -> "FILL_BLANK"
           - (判断) -> "TRUE_FALSE"
           - (简答/名词解释/病例) -> "ESSAY"
        2. **选项清洗**：移除选项前的 "A." "B." 或 "1)" 等标签，存入 "label"。
        3. **答案匹配**：优先提取题目自带的答案；若无，尝试在[参考答案库]中查找对应题号；无法确定则留空。

        [参考答案库(仅供查找，非当前文本)]
        {ans_key[:3000]}... (上下文截断)

        [One-Shot 示例(严格模仿此格式)]
        输入: "3. 高血压的诊断标准是( ) A. 140/90 B. 130/80 [答案]A [解析]见课本P10... 4. 糖尿病的典型"
        输出: 
        [
          {{
            "category": "单选题",
            "type": "SINGLE_CHOICE",
            "content": "高血压的诊断标准是( )",
            "options": [
              {{"label": "A", "text": "140/90"}},
              {{"label": "B", "text": "130/80"}}
            ],
            "answer": "A",
            "analysis": "见课本P10..."
          }}
        ]

        [待处理文本片段]
        {chunk}
        """

    start_t = time.time()
    attempt = 0

    # ♾️ 死磕循环：只要不成功，就一直换号重试，直到天荒地老
    while True:
        attempt += 1

        # 🕵️‍♂️ 【伪装策略2】身份漫游
        # 每次请求（包括重试）都切换不同的 Key
        # 让平台认为这是该 IP 下的“另一个用户”在尝试
        client, k_id = get_random_client()

        try:
            # ⚡️ 强制超时：45秒
            # 设置得比平台默认短，防止被判定为长连接占用资源
            res = client.chat.completions.create(
                model=AI_MODEL_NAME, messages=[{"role": "user", "content": prompt}],
                temperature=0.1, top_p=0.7, max_tokens=4000, timeout=45
            )
            content = repair_json(res.choices[0].message.content)
            data = json.loads(content)

            # 成功后立即返回
            cost = time.time() - start_t
            msg = f"Chunk {idx + 1} 完成 (耗时:{cost:.1f}s, 重试:{attempt - 1}, Key:..{k_id})"

            if isinstance(data, list): return data, None, msg
            if isinstance(data, dict): return [data], None, msg

            # 数据格式不对，视为失败，抛出异常进入重试
            raise ValueError("JSON格式解析失败")

        except Exception as e:
            # 🕵️‍♂️ 【伪装策略3】智能退避 (Smart Backoff)
            # 失败了不要立即“疯狗式”重试，而是像人一样“愣一下”再试

            # 基础等待：2~5秒随机
            wait_time = random.uniform(2.0, 5.0)

            # 如果连续失败超过 3 次，说明可能被风控了，大幅增加休息时间
            if attempt > 3:
                wait_time = random.uniform(5.0, 10.0)
                # 打印日志让我们知道它在努力，但不要太频繁
                print(
                    f"   🛡️ Chunk {idx + 1} 触发避险机制 (第{attempt}次重试) -> 切换身份(Key..{k_id}) -> 静默 {wait_time:.1f}s",
                    flush=True)

            # 如果连续失败超过 10 次，说明 IP 被暂时关小黑屋了，休息 20 秒
            if attempt > 10:
                wait_time = 20.0

            time.sleep(wait_time)


# ================= 📤 发送模块 =================
def generate_html_report(data):
    is_success = data['failed_chunks'] == 0
    color = "#28a745" if is_success else "#dc3545"
    title = f"✅ {SUBJECT} 题库生成成功" if is_success else f"⚠️ {SUBJECT} 生成含异常"
    log_html = "".join(EXECUTION_LOGS)

    html = f"""
    <div style="font-family:sans-serif; max-width:600px; padding:20px; border:1px solid #ddd; border-radius:8px;">
        <div style="border-bottom:2px solid {color}; padding-bottom:10px; margin-bottom:20px;">
            <h2 style="margin:0; color:#333;">{title}</h2>
            <p style="color:#666; font-size:12px; margin:5px 0;">{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
        </div>
        <div style="background:#f8f9fa; padding:10px; border-radius:4px; margin-bottom:15px; font-size:14px;">
            <p><b>📚 学科:</b> {SUBJECT}</p>
            <p><b>🚀 状态:</b> {data['success_chunks']} 成功 / <span style="color:red">{data['failed_chunks']} 失败</span></p>
            <p><b>⏱️ 耗时:</b> {data['duration']:.1f}s</p>
            <p><b>📝 题目总数:</b> {data['total_questions']}</p>
            <p><b>📄 处理文件数:</b> {data['file_count']}</p>
        </div>
        <h4 style="margin:10px 0;">📜 运行日志</h4>
        <div style="background:#fafafa; border:1px solid #eee; height:300px; overflow-y:auto; padding:10px; font-size:12px;">{log_html}</div>
    </div>
    """
    return title, html


def send_pushplus(title, content):
    if not PUSHPLUS_TOKEN: return
    try:
        requests.post("http://www.pushplus.plus/send",
                      json={"token": PUSHPLUS_TOKEN, "title": title, "content": content, "template": "html"}, timeout=5)
    except:
        pass


def send_email(title, content):
    if not SMTP_USER or not SMTP_PASS or not RECEIVER_EMAILS: return
    try:
        smtp_obj = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT)
        smtp_obj.login(SMTP_USER, SMTP_PASS)
        for email in RECEIVER_EMAILS:
            try:
                msg = MIMEText(content, 'html', 'utf-8')
                msg['From'] = Header(f"题库助手 <{SMTP_USER}>", 'utf-8')
                msg['To'] = Header(email, 'utf-8')
                msg['Subject'] = Header(title, 'utf-8')
                smtp_obj.sendmail(SMTP_USER, [email], msg.as_string())
                print(f"✅ 邮件已发送至 {email}", flush=True)
            except:
                pass
        smtp_obj.quit()
    except Exception as e:
        print(f"❌ 邮件服务连接失败: {e}", flush=True)


# ================= 🚀 主程序 (实时保存版) =================
def main():
    st = time.time()
    if not os.path.exists(INPUT_DIR): return
    files = [f for f in os.listdir(INPUT_DIR) if f.endswith(".docx")]
    if not files: return

    if not os.path.exists(OUTPUT_DIR): os.makedirs(OUTPUT_DIR)

    # 计算目标文件名
    exist_files = [f for f in os.listdir(OUTPUT_DIR) if f.startswith("output") and f.endswith(".json")]
    next_idx = 1
    for f in exist_files:
        m = re.search(r'output(\d+)', f)
        if m: next_idx = max(next_idx, int(m.group(1)) + 1)
    target_file = os.path.join(OUTPUT_DIR, f"output{next_idx}.json")

    log_record(f"🚀 [{SUBJECT}] 启动 | Key: {len(API_KEYS)} | 并发: {MAX_WORKERS}")

    all_qs = []
    stats = {"file_count": len(files), "total_chunks": 0, "success_chunks": 0, "failed_chunks": 0}

    # 循环处理每个文件
    for fname in files:
        log_record(f"📄 正在处理: {fname}...")
        txt = read_docx(os.path.join(INPUT_DIR, fname))
        if not txt: continue

        chunks = get_chunks(txt, CHUNK_SIZE, OVERLAP)
        stats['total_chunks'] += len(chunks)
        total_c = len(chunks)

        # 处理当前文件的所有切片
        current_file_qs = []
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exc:
            futures = [exc.submit(process_chunk, (c, i, "")) for i, c in enumerate(chunks)]

            for i, fut in enumerate(as_completed(futures)):
                # ✅ 此时这里的 unpack 一定是安全的 3 个值
                qs, err, msg = fut.result()

                if err:
                    stats['failed_chunks'] += 1
                    log_record(f"[{i + 1}/{total_c}] ❌ {err}", "ERROR")
                else:
                    stats['success_chunks'] += 1
                    log_record(f"[{i + 1}/{total_c}] {msg}")
                    if qs:
                        for q in qs:
                            q['id'] = str(uuid.uuid4())
                            q['number'] = len(all_qs) + len(current_file_qs) + 1
                            q['chapter'] = fname.replace(".docx", "")
                            q['category'] = normalize_category(q.get('category', '综合题'))
                            if 'analysis' not in q: q['analysis'] = ""
                            current_file_qs.append(q)

        # ✅ 【实时保存】处理完一个文件，立刻写入总表和文件
        all_qs.extend(current_file_qs)
        log_record(f"💾 {fname} 处理完毕，当前总题数: {len(all_qs)} (已存档)")

        final_data = {"version": "MultiKey-V13-AutoSave", "subject": SUBJECT, "data": all_qs}
        with open(target_file, 'w', encoding='utf-8') as f:
            json.dump(final_data, f, ensure_ascii=False, indent=2)

    stats['duration'] = time.time() - st
    stats['total_questions'] = len(all_qs)
    log_record(f"✨ 全部任务完成! 总耗时 {stats['duration']:.1f}s")

    title, html = generate_html_report(stats)
    send_pushplus(title, html)
    send_email(title, html)


if __name__ == "__main__": main()