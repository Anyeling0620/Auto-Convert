import json
import os
import time
import requests
import datetime
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
KEY_INDEX = APP_CONFIG.get("key_index", 0)

# ================= 🔑 密钥逻辑 =================
KEY_POOL_STR = os.getenv("ZHIPU_KEY_POOL", "")
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN")
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "local")


def get_api_key():
    if not KEY_POOL_STR: return None
    keys = [k.strip() for k in KEY_POOL_STR.split(',') if k.strip()]
    if not keys: return None
    if KEY_INDEX >= len(keys): return keys[0]
    return keys[KEY_INDEX]


ZHIPU_API_KEY = get_api_key()
AI_MODEL_NAME = "glm-4-flash"
MAX_WORKERS = 20

if not ZHIPU_API_KEY:
    print("❌ 错误：无法获取 API Key")
    exit(1)

client = ZhipuAI(api_key=ZHIPU_API_KEY)


# ================= 📧 报表推送 =================
def send_validation_report(data):
    if not PUSHPLUS_TOKEN: return

    has_doubts = len(data['doubt_list']) > 0
    has_errors = len(data['api_errors']) > 0

    color = "#ffc107" if has_doubts else "#28a745"
    if has_errors: color = "#dc3545"

    title = f"🔍 质检完成：发现 {len(data['doubt_list'])} 处存疑"

    html = f"""
    <div style="font-family:sans-serif; max-width:600px; padding:20px; border:1px solid #ddd; border-radius:8px;">
        <div style="border-bottom:2px solid {color}; padding-bottom:10px; margin-bottom:20px;">
            <h2 style="margin:0; color:#333;">{title}</h2>
            <p style="color:#666; font-size:12px; margin:5px 0;">{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}</p>
        </div>
        <div style="background:#f8f9fa; padding:10px; border-radius:4px; margin-bottom:15px; font-size:14px;">
            <p style="margin:4px 0;"><b>📚 学科:</b> {SUBJECT}</p>
            <p style="margin:4px 0;"><b>📁 文件:</b> {data['filename']}</p>
        </div>
        <ul style="padding-left:20px; margin-bottom:20px;">
            <li>📊 校验总数: <b>{data['total']}</b> 题</li>
            <li>🤔 存疑数量: <b style="color:#d39e00;">{len(data['doubt_list'])}</b> 题</li>
            <li>❌ API失败: {len(data['api_errors'])} 次</li>
        </ul>
    """

    # 存疑详情
    if data['doubt_list']:
        # 只显示前 50 个题号，防止消息过长
        display_list = data['doubt_list'][:50]
        more_count = len(data['doubt_list']) - 50
        num_str = ", ".join(map(str, display_list))
        if more_count > 0: num_str += f", ... (还有 {more_count} 个)"

        html += f"""
        <div style="background:#fff3cd; padding:10px; border-radius:4px; border:1px solid #ffeeba; margin-bottom:15px;">
            <h4 style="margin-top:0; color:#856404;">🤔 存疑题号列表</h4>
            <p style="color:#856404; font-size:13px; word-break:break-all;">{num_str}</p>
        </div>
        """

    # API 错误详情
    if data['api_errors']:
        html += f"""
        <div style="background:#f8d7da; padding:10px; border-radius:4px; border:1px solid #f5c6cb;">
            <h4 style="margin-top:0; color:#721c24;">❌ API 调用错误</h4>
            <ul style="padding-left:20px; color:#721c24; font-size:12px;">
                {''.join([f'<li>{e}</li>' for e in data['api_errors'][:10]])}
            </ul>
        </div>
        """

    html += "</div>"

    requests.post("http://www.pushplus.plus/send", json={
        "token": PUSHPLUS_TOKEN, "title": f"[{SUBJECT}] 质检报告", "content": html, "template": "html"
    }, timeout=5)


# ================= 🚀 校验逻辑 =================
def validate_single(question):
    # 构造清晰的选项文本，方便 AI 阅读
    options_text = ""
    if question.get('options'):
        options_text = "\n".join([f"{opt['label']}. {opt['text']}" for opt in question['options']])

    prompt = f"""
    [系统角色]
    你是一位资深的**{SUBJECT}**学科专家和试题审核员。
    你的任务是审核一道刚刚从文档中提取出来的题目，判断其“参考答案”是否存在明显错误。

    [待审核题目详情]
    --------------------------------------------------
    【学科章节】：{question.get('chapter', '未知章节')}
    【题型分类】：{question.get('category', '未知题型')}
    【题目内容】：
    {question['content']}
    
    【候选选项】：
    {options_text}
    --------------------------------------------------

    [给出的参考答案]
    {question['answer']}

    [审核判罚标准]
    1. **事实性错误 (Fatal Error)**：参考答案违反了学科公理、常识或标准指南。
       - 例如：医学中使用了禁忌药；数学中 1+1=3；计算机中死锁条件错误。
       - 判定：必须报错。
    2. **逻辑/格式错误 (Logic Error)**：
       - 单选题给出了多个答案（如 "AB"）。
       - 多选题只给了一个答案（如 "A"）。
       - 判断题答案不是对/错。
       - 判定：必须报错。
    3. **主观题宽容原则**：
       - 对于“简答题”、“论述题”、“编程题”，只要参考答案的逻辑通顺、言之有理，即视为正确。不要吹毛求疵。

    [输出指令]
    请仅输出以下两种格式之一，不要包含其他废话：

    格式 A（认为正确）：
    CORRECT

    格式 B（认为存疑）：
    DOUBT: [此处简短说明错误理由，并给出你认为的正确答案]

    """
    try:
        res = client.chat.completions.create(
            model=AI_MODEL_NAME, messages=[{"role": "user", "content": prompt}],
            temperature=0.1, max_tokens=200
        )
        content = res.choices[0].message.content.strip()
        if "DOUBT" in content or "存疑" in content:
            reason = content.replace("DOUBT:", "").replace("DOUBT", "").strip()
            return True, f"【答案存疑】AI提示：{reason}\n\n", None
        return False, "", None
    except Exception as e:
        return False, "", str(e)


def main():
    if not os.path.exists("last_generated_file.txt"): return
    with open("last_generated_file.txt", "r") as f:
        target_file = f.read().strip()
    if not os.path.exists(target_file): return

    print(f"🕵️‍♂️ 启动质检 | 目标: {target_file}")
    with open(target_file, 'r', encoding='utf-8') as f:
        data = json.load(f)

    questions = data['data']
    stats = {
        "filename": os.path.basename(target_file),
        "total": len(questions),
        "doubt_list": [],
        "api_errors": []
    }

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as exc:
        futures = {exc.submit(validate_single, q): i for i, q in enumerate(questions)}
        for fut in tqdm(as_completed(futures), total=len(questions)):
            idx = futures[fut]
            try:
                is_doubt, reason, err = fut.result()
                if err:
                    stats['api_errors'].append(f"第 {questions[idx]['number']} 题: {err}")
                elif is_doubt:
                    stats['doubt_list'].append(questions[idx]['number'])
                    questions[idx]['analysis'] = reason + questions[idx].get('analysis', "")
            except:
                pass

    data['data'] = questions
    data['source'] += " + Validated"
    with open(target_file, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

    print(f"✅ 质检完成！存疑: {len(stats['doubt_list'])}")
    send_validation_report(stats)


if __name__ == "__main__": main()