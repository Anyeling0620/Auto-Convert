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

# 📧 邮件配置 (请在 GitHub Secrets 或 环境变量中配置)
# 如果没有配置，脚本会自动跳过邮件发送
SMTP_HOST = os.getenv("SMTP_HOST", "smtp.163.com")  # =smtp.163.com
SMTP_PORT = int(os.getenv("SMTP_PORT", 465))        # SSL端口通常是 465
SMTP_USER = os.getenv("SMTP_USER")                  # 发送方邮箱账号
SMTP_PASS = os.getenv("SMTP_PASS")                  # 发送方邮箱授权码
RECEIVER_EMAILS_STR = os.getenv("RECEIVER_EMAILS", "")
if RECEIVER_EMAILS_STR:
    # 使用正则切分，兼容 Windows/Linux 换行符，逗号等
    RECEIVER_EMAILS = [e.strip() for e in re.split(r'[,\n\s]+', RECEIVER_EMAILS_STR) if e.strip()]
else:
    RECEIVER_EMAILS = []

# ================= 🔑 密钥负载均衡池 (核心升级) =================
KEY_POOL_STR = os.getenv("ZHIPU_KEY_POOL", "")
if KEY_POOL_STR:
    API_KEYS = [k.strip() for k in re.split(r'[,\n\s]+', KEY_POOL_STR) if k.strip()]
else:
    API_KEYS = []

if not API_KEYS:
    print("❌ 严重错误：ZHIPU_KEY_POOL 为空！请在 GitHub Secrets 中配置。")
    if __name__ == "__main__": exit(1)

print(f"🔥 密钥池加载成功：共 {len(API_KEYS)} 个 Key")


def get_random_client():
    """随机抽取一个 Key 创建客户端"""
    selected_key = random.choice(API_KEYS)
    return ZhipuAI(api_key=selected_key), selected_key[-4:]

# ================= ⚙️ 性能策略优化 (关键修改) =================

# 1. 并发数调整：保守策略
# 即使有11个Key，也不要开16并发。建议比例 1:0.5 (2个Key养1个线程)
# 这样能确保当一个Key被限流时，还有充裕的空闲Key可用
calculated_workers = max(1, len(API_KEYS) // 2)
MAX_WORKERS = 12
# 强制封顶，防止 GitHub Action 内存溢出或被 API 服务商封锁
if MAX_WORKERS > 16: MAX_WORKERS = 16

# 2. 超时与重试调整
# 减少重试次数，增加单次等待耐心
AI_MODEL_NAME = "glm-4-flash"
CHUNK_SIZE = 1000
OVERLAP = 100
MAX_RETRIES = 5  # ⬇️ 降级：从5次改为3次 (Fail fast)
API_TIMEOUT = 60  # ⬆️ 升级：从40s改为60s (给AI更多思考时间，减少伪性超时)
RETRY_DELAY = 1  # ⬆️ 新增：重试前的冷却时间 (秒)

PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN")
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "local")

# ================= 📝 全局日志记录器 =================
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


# ================= 📤 发送模块 (支持群发) =================
def generate_html_report(data):
    is_success = data['failed_chunks'] == 0
    color = "#28a745" if is_success else "#dc3545"
    title = "✅ 题库生成成功" if is_success else "⚠️ 生成存在异常"

    log_html = "".join(EXECUTION_LOGS)

    html = f"""
    <div style="font-family:sans-serif; max-width:600px; padding:20px; border:1px solid #ddd; border-radius:8px;">
        <div style="border-bottom:2px solid {color}; padding-bottom:10px; margin-bottom:20px;">
            <h2 style="margin:0; color:#333;">{title}</h2>
            <p style="color:#666; font-size:12px; margin:5px 0;">{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
        </div>
        <div style="background:#f8f9fa; padding:10px; border-radius:4px; margin-bottom:15px; font-size:14px;">
            <p style="margin:4px 0;"><b>📚 学科:</b> {SUBJECT}</p>
            <p style="margin:4px 0;"><b>🔑 密钥池:</b> {len(API_KEYS)} 个</p>
            <p style="margin:4px 0;"><b>🚀 状态:</b> {data['success_chunks']} 成功 / <span style="color:red">{data['failed_chunks']} 失败</span></p>
            <p style="margin:4px 0;"><b>⏱️ 总耗时:</b> {data['duration']:.1f}s</p>
            <p style="margin:4px 0;"><b>📝 题目数:</b> {data['total_questions']}</p>
        </div>

        <h4 style="margin:10px 0;">📜 运行日志 (滚动查看)</h4>
        <div style="background:#fafafa; border:1px solid #eee; height:300px; overflow-y:auto; padding:10px; font-size:12px; font-family:monospace;">
            {log_html}
        </div>
    </div>
    """
    return title, html


def send_email(title, content):
    if not SMTP_USER or not SMTP_PASS:
        print("⚠️ 未配置 SMTP，跳过发送邮件", flush=True)
        return

    if not RECEIVER_EMAILS:
        print("⚠️ 未配置接收邮箱 (RECEIVER_EMAILS)，跳过发送", flush=True)
        return

    try:
        # 建立一次连接，循环发送
        smtp_obj = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT)
        smtp_obj.login(SMTP_USER, SMTP_PASS)

        for email in RECEIVER_EMAILS:
            try:
                message = MIMEText(content, 'html', 'utf-8')
                message['From'] = Header(f"题库助手 <{SMTP_USER}>", 'utf-8')
                message['To'] = Header(email, 'utf-8')
                message['Subject'] = Header(title, 'utf-8')

                smtp_obj.sendmail(SMTP_USER, [email], message.as_string())
                print(f"✅ 邮件已发送至 {email}", flush=True)
            except Exception as e:
                print(f"❌ 发送至 {email} 失败: {e}", flush=True)

        smtp_obj.quit()
    except Exception as e:
        print(f"❌ 邮件服务连接失败: {e}", flush=True)

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
            <p style="margin:4px 0;"><b>🚀 并发:</b> {MAX_WORKERS} 线程 (稳健模式)</p>
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

def send_pushplus(title, content):
    if not PUSHPLUS_TOKEN: return
    try:
        requests.post("http://www.pushplus.plus/send",
                      json={"token": PUSHPLUS_TOKEN, "title": title, "content": content, "template": "html"}, timeout=5)
        print("✅ PushPlus 推送成功", flush=True)
    except Exception as e:
        print(f"❌ PushPlus 推送失败: {e}", flush=True)

# ================= 🛠️ 核心逻辑 =================
def read_docx(file_path):
    if not os.path.exists(file_path): return ""
    try:
        doc = Document(file_path)
        return "\n".join([p.text.strip() for p in doc.paragraphs if p.text.strip()])
    except:
        return ""


def get_chunks(text, size, overlap):
    chunks = []
    start = 0
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
    client, k_id = get_random_client()
    try:
        res = client.chat.completions.create(
            model=AI_MODEL_NAME, messages=[{"role": "user", "content": "提取参考答案，纯文本列表。\n\n" + txt[:10000]}],
            temperature=0.1, timeout=60
        )
        return res.choices[0].message.content
    except:
        return ""


def process_chunk(args):
    chunk, idx, ans_key = args
    time.sleep(random.uniform(0.5, 2.0))
    prompt = f"""
            [系统角色设定]
            你是由 Python 脚本调用的“全学科试题数据结构化引擎”。
            **你不是聊天助手，严禁输出任何寒暄语、解释性文字或 Markdown 代码标记（如 ```json）。**
            你的唯一任务是将输入的非结构化文本切片，精准解析为符合 Schema 定义的 JSON 数组。

            [当前处理学科]
            - 学科名称：**{SUBJECT}**
            - 学科背景：{DESC}

            [全局上下文：参考答案库]
            ---------------------------------------------------------------------
            {ans_key[:5000]} ...
            ---------------------------------------------------------------------

            [严格执行守则]
            1. **边界截断处理**：丢弃切片首尾不完整的残缺段落。
            2. **答案匹配逻辑**：
               - Level 1: 题目自带答案。
               - Level 2: 匹配【参考答案库】中的题号。
               - Level 3: 若无法确定，answer 字段留空。严禁瞎编。
            3. **清洗规则**：去除题号、选项标签(A/B/C/D)，保留公式。
            4. **题型归一化**：映射为标准题型（单选题/多选题/填空题/简答题等）。

            [输出格式规范 (JSON Schema)]
            [
              {{
                "category": "String",
                "type": "Enum (SINGLE_CHOICE / MULTI_CHOICE / TRUE_FALSE / FILL_BLANK / ESSAY)",
                "content": "String (题干)",
                "options": [
                   {{"label": "A", "text": "内容..."}}
                ],
                "answer": "String",
                "analysis": "String"
              }}
            ]

            [待处理文本切片]
            {chunk}
            """

    start_t = time.time()
    last_err = ""

    for i in range(MAX_RETRIES):
        client, k_id = get_random_client()
        try:
            res = client.chat.completions.create(
                model=AI_MODEL_NAME, messages=[{"role": "user", "content": prompt}],
                temperature=0.1, top_p=0.7, max_tokens=4000, timeout=API_TIMEOUT
            )
            content = repair_json(res.choices[0].message.content)
            try:
                data = json.loads(content)
                cost = time.time() - start_t
                if cost > 15:  # 稍微降低日志阈值
                    tqdm.write(f"   ✅ Chunk {idx + 1} 完成 (耗时: {cost:.1f}s) - Key..{k_id}")

                if isinstance(data, list): return data, None
                if isinstance(data, dict): return [data], None
                raise ValueError("Format Error")
            except:
                raise ValueError("JSON Decode Failed")

        except Exception as e:
            last_err = str(e)
            cost = time.time() - start_t
            err_type = "⏱️ 超时" if "timed out" in str(e) else "⚠️ 报错"

            tqdm.write(
                f"   {err_type} Chunk {idx + 1} (Key..{k_id}) -> 重试 {i + 1}/{MAX_RETRIES} (已耗时 {cost:.1f}s)")

            # 【核心修改】退避策略：失败后睡 3 秒，不再疯狗式重试
            time.sleep(RETRY_DELAY)

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

    log_record(f"🚀 [{SUBJECT}] 启动 | Key: {len(API_KEYS)} | 并发: {MAX_WORKERS}")
    if RECEIVER_EMAILS:
        log_record(f"📧 邮件将发送给: {len(RECEIVER_EMAILS)} 位接收者")

    all_qs = []
    stats = {"file_count": len(files), "total_chunks": 0, "success_chunks": 0, "failed_chunks": 0}

    for fname in files:
        log_record(f"📄 处理文件: {fname}")
        txt = read_docx(os.path.join(INPUT_DIR, fname))
        if not txt: continue

        chunks = get_chunks(txt, CHUNK_SIZE, OVERLAP)
        stats['total_chunks'] += len(chunks)
        total_c = len(chunks)

        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exc:
            futures = [exc.submit(process_chunk, (c, i, "")) for i, c in enumerate(chunks)]

            for i, fut in enumerate(as_completed(futures)):
                qs, err, msg = fut.result()
                if err:
                    stats['failed_chunks'] += 1
                    log_record(f"[{i + 1}/{total_c}] ❌ {err}", "ERROR")
                else:
                    stats['success_chunks'] += 1
                    log_record(f"[{i + 1}/{total_c}] {msg}")
                    if qs:
                        for q in qs:
                            q['id'] = str(uuid.uuid4());
                            q['number'] = len(all_qs) + 1
                            q['chapter'] = fname.replace(".docx", "");
                            q['category'] = normalize_category(q.get('category', '综合题'))
                            if 'analysis' not in q: q['analysis'] = ""
                            all_qs.append(q)

    final = {"version": "MultiKey-V12-EmailGroup", "subject": SUBJECT, "data": all_qs}
    with open(target_file, 'w', encoding='utf-8') as f:
        json.dump(final, f, ensure_ascii=False, indent=2)

    stats['duration'] = time.time() - st
    stats['total_questions'] = len(all_qs)
    log_record(f"✨ 完成! 耗时 {stats['duration']:.1f}s, 提取 {len(all_qs)} 题")

    title, html = generate_html_report(stats)
    send_pushplus(title, html)
    send_email(title, html)


if __name__ == "__main__": main()