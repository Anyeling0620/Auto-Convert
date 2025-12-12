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
KEY_INDEX = APP_CONFIG.get("key_index", 0)
INPUT_DIR = "input"
OUTPUT_DIR = "output"
MAX_WORKERS = APP_CONFIG.get("max_workers", 16)

# ================= 🔑 环境与密钥 =================
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "local-dev")
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "Local/Repo")
KEY_POOL_STR = os.getenv("ZHIPU_KEY_POOL", "")
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN")


def get_api_key():
    if not KEY_POOL_STR: return None
    keys = [k.strip() for k in KEY_POOL_STR.split(',') if k.strip()]
    if not keys: return None
    if KEY_INDEX >= len(keys): return keys[0]
    return keys[KEY_INDEX]


ZHIPU_API_KEY = get_api_key()
AI_MODEL_NAME = "glm-4-flash"
CHUNK_SIZE = 2000;
OVERLAP = 200;
MAX_RETRIES = 5;
API_TIMEOUT = 120

if not ZHIPU_API_KEY:
    print("❌ 错误：无法获取 API Key")
    exit(1)

client = ZhipuAI(api_key=ZHIPU_API_KEY)


# ================= 📧 报表推送模块 =================
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
            <p style="margin:4px 0;"><b>🌿 分支:</b> {GITHUB_REF_NAME}</p>
            <p style="margin:4px 0;"><b>🤖 模型:</b> {AI_MODEL_NAME}</p>
        </div>
        <ul style="padding-left:20px; margin-bottom:20px;">
            <li>⏱️ 耗时: <b>{data['duration']:.1f}s</b></li>
            <li>📄 文件: {data['file_count']} 个</li>
            <li>📝 题目: <b style="color:#007bff; font-size:16px;">{data['total_questions']}</b> 道</li>
            <li>🧩 切片: 成功 {data['success_chunks']} / 失败 <b style="color:red;">{data['failed_chunks']}</b></li>
        </ul>
    """

    if data['errors']:
        html += "<div style='background:#fff3cd; padding:10px; border-radius:4px; border:1px solid #ffeeba;'>"
        html += "<h4 style='margin-top:0; color:#856404;'>⚠️ 异常详情</h4><ul style='padding-left:20px; color:#856404; font-size:13px;'>"
        for err in data['errors']:
            html += f"<li style='margin-bottom:4px;'>{err}</li>"
        html += "</ul></div>"

    html += "</div>"

    requests.post("http://www.pushplus.plus/send", json={
        "token": PUSHPLUS_TOKEN, "title": f"[{SUBJECT}] 生成报告", "content": html, "template": "html"
    }, timeout=5)


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

    last_err = ""
    for i in range(MAX_RETRIES):
        try:
            res = client.chat.completions.create(
                model=AI_MODEL_NAME, messages=[{"role": "user", "content": prompt}],
                temperature=0.1, top_p=0.7, max_tokens=4000, timeout=API_TIMEOUT
            )
            content = repair_json(res.choices[0].message.content)
            try:
                data = json.loads(content)
                if isinstance(data, list): return data, None
                if isinstance(data, dict): return [data], None
                return [], f"Chunk {idx + 1}: JSON格式异常"
            except:
                continue
        except Exception as e:
            last_err = str(e)
            time.sleep((2 ** i) + random.random())

    return [], f"Chunk {idx + 1} 失败 (API: {last_err})"


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

    print(f"🚀 [{SUBJECT}] 启动 | 分支: {GITHUB_REF_NAME}")

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

    final = {"version": "V7-Report", "subject": SUBJECT, "data": all_qs}
    with open(target_file, 'w', encoding='utf-8') as f:
        json.dump(final, f, ensure_ascii=False, indent=2)
    with open("last_generated_file.txt", "w") as f:
        f.write(target_file)

    stats['duration'] = time.time() - st
    stats['total_questions'] = len(all_qs)
    print(f"\n✨ 完成！提取 {len(all_qs)} 题")
    send_report(stats)


if __name__ == "__main__": main()