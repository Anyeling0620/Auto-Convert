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
MAX_WORKERS = 16  # 并发数
AI_MODEL_NAME = "glm-4-flash"
CHUNK_SIZE = 1000  # 切片大小
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


# ✅ 修复后的 process_chunk (确保一定返回3个值)
def process_chunk(args):
    chunk, idx, ans_key = args
    start_delay = (idx % 16) * 0.5
    time.sleep(start_delay)

    prompt = f"""
            [系统角色设定]
            你是由 Python 脚本调用的“全学科试题数据结构化引擎”。
            **你不是聊天助手，严禁输出任何寒暄语、解释性文字或 Markdown 代码标记（如 ```json）。**
            你的唯一任务是将输入的非结构化文本切片，精准解析为符合 Schema 定义的 JSON 数组。

            [当前处理学科]
            - 学科名称：**{SUBJECT}**
            - 学科背景：{DESC}
            （请利用学科背景知识来辅助判断题型，例如：医学常出现 A1/病例分析；计算机常出现编程/算法；数学常出现证明/计算）

            [全局上下文：参考答案库]
            ---------------------------------------------------------------------
            {ans_key[:5000]} ... (若过长已自动截断，仅供查阅)
            ---------------------------------------------------------------------

            [严格执行守则 (Chain of Constraints)]

            1. **边界截断处理 (最高优先级)**：
               - 输入文本是长文档的一个切片。
               - **直接丢弃**切片开头处不完整的残缺段落（例如：只有选项没有题干）。
               - **直接丢弃**切片末尾处不完整的残缺段落（例如：只有题干没有选项）。
               - 只提取中间语义完整的题目。

            2. **答案匹配逻辑 (三级瀑布流)**：
               - **Level 1 (自带)**：优先提取题目文本内部自带的答案（例如：括号内的字母、题干末尾的答案、选项下方的“【答案】”）。
               - **Level 2 (查表)**：提取题目中的【题号】（如 "53."），去上方的 [参考答案库] 中查找对应题号的答案。
               - **Level 3 (留空)**：如果 Level 1 和 Level 2 都失败，`answer` 字段必须留空字符串 ""。**严禁根据题目内容自己做题！严禁随机生成！**

            3. **文本清洗规则**：
               - **Content 清洗**：移除题干开头的题号（例如："1. 下列哪项..." -> "下列哪项..."）。
               - **Option 清洗**：移除选项开头的标签（例如："A. 阿司匹林" -> label:"A", text:"阿司匹林"）。
               - **特殊符号**：保留代码块、数学公式（LaTeX）、化学式原本的格式，不要随意转义。

            4. **题型归一化映射 (Category Mapping)**：
               - **医学专用**：
                 * 5个选项(A-E)单选 -> "A1型题" 或 "A2型题"
                 * 共用题干/配伍 -> "B1型题"
                 * 多选题 -> "X型题"
                 * 病例描述/诊断 -> "病例分析题"
               - **理工/计算机专用**：
                 * 代码补全/算法实现 -> "编程题"
                 * 数值计算/公式推导 -> "计算题"
                 * 逻辑证明 -> "证明题"
                 * 系统设计/应用场景 -> "应用题"
               - **通用基础**：
                 * 4个选项单选 -> "单选题"
                 * 多个正确答案/不定项 -> "多选题"
                 * 对/错, T/F -> "判断题"
                 * 下划线/括号填空 -> "填空题"
                 * 无选项主观问答 -> "简答题"
                 * 名词解释 -> "名词解释题"

            [输出格式规范 (JSON Schema)]
            必须返回一个纯净的 JSON Array，包含以下字段：
            [
              {{
                "category": "String (必须是上述映射表中的标准名称)",
                "type": "Enum (SINGLE_CHOICE / MULTI_CHOICE / TRUE_FALSE / FILL_BLANK / ESSAY)",
                "content": "String (清洗后的完整题干)",
                "options": [
                   {{"label": "A", "text": "选项内容..."}},
                   {{"label": "B", "text": "选项内容..."}}
                ],
                "answer": "String (例如 'A', 'ABC', 'True', 'void main()...')",
                "analysis": "String (如果文本中有解析则提取，否则留空)"
              }}
            ]

            [待处理文本切片]
            {chunk}
            """

    start_t = time.time()
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

                # ✅ 构造成功日志
                msg = f"Chunk {idx + 1} 完成 (耗时:{cost:.1f}s, Key:..{k_id})"

                # ✅ 统一返回 3 个值
                if isinstance(data, list): return data, None, msg
                if isinstance(data, dict): return [data], None, msg
                raise ValueError("Format Error")
            except:
                raise ValueError("JSON Decode Failed")

        except Exception as e:
            cost = time.time() - start_t
            err_msg = str(e)[:50]
            # 记录警告但不返回
            # log_record(f"Chunk {idx+1} 重试 {i+1}/{MAX_RETRIES}: {err_msg}", "WARN")
            time.sleep(RETRY_DELAY)

    # ✅ 失败时也必须返回 3 个值
    return [], f"Chunk {idx + 1} 彻底失败", ""


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