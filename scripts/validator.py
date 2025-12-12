import json
import os
import time
from zhipuai import ZhipuAI
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

# ================= 🛡️ 配置加载模块 =================
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
# 【核心修复】读取 config 里的 key_index
KEY_INDEX = APP_CONFIG.get("key_index", 0)

# ================= 🔑 密钥池解析逻辑 =================
# 读取环境变量里的整个字符串
KEY_POOL_STR = os.getenv("ZHIPU_KEY_POOL", "")


def get_api_key():
    """根据 Config 里的 index 从环境变量池中提取 Key"""
    if not KEY_POOL_STR:
        print("❌ 校验器错误：环境变量 ZHIPU_KEY_POOL 未设置或为空！")
        return None

    # 按逗号切割
    keys = [k.strip() for k in KEY_POOL_STR.split(',') if k.strip()]

    if not keys:
        print("❌ 校验器错误：密钥池中没有有效的 Key！")
        return None

    # 检查索引是否越界
    if KEY_INDEX >= len(keys):
        print(f"⚠️ 校验器警告：config.json 请求第 {KEY_INDEX} 个 Key，但池子里只有 {len(keys)} 个。")
        print(f"🔄 自动回滚使用第 1 个 Key。")
        return keys[0]

    print(f"🕵️‍♂️ 校验器已选中第 {KEY_INDEX} 个 Key (Index {KEY_INDEX})。")
    return keys[KEY_INDEX]


# 获取最终的 Key
ZHIPU_API_KEY = get_api_key()
AI_MODEL_NAME = "glm-4-flash"
MAX_WORKERS = 20  # 校验速度快，并发拉高

if not ZHIPU_API_KEY:
    print("❌ 严重错误：无法获取有效的 ZHIPU_API_KEY，校验终止。")
    exit(1)

client = ZhipuAI(api_key=ZHIPU_API_KEY)


# =======================================================

def validate_single_question(question):
    """
    使用 AI 对单个题目进行逻辑/事实校验
    """
    options_text = ""
    if question.get('options'):
        options_text = "\n".join([f"{opt['label']}. {opt['text']}" for opt in question['options']])

    prompt = f"""
    [任务]
    你是一位**{SUBJECT}**学科的审题专家。
    请检查以下题目的参考答案是否正确。

    [题目]
    题型: {question.get('category')}
    内容: {question['content']}
    选项: 
    {options_text}

    [参考答案]
    {question['answer']}

    [输出]
    正确回复 "CORRECT"。
    存疑回复 "DOUBT: 理由"。
    """

    try:
        response = client.chat.completions.create(
            model=AI_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=200
        )
        result = response.choices[0].message.content.strip()

        if result.startswith("DOUBT") or "存疑" in result:
            reason = result.replace("DOUBT:", "").replace("DOUBT", "").strip()
            return True, f"【答案存疑】AI({SUBJECT}专家)提示：{reason}\n\n"
        return False, ""

    except Exception:
        return False, ""


def main():
    # 1. 读取生成脚本留下的文件名
    if not os.path.exists("last_generated_file.txt"):
        print("❌ 找不到 last_generated_file.txt，跳过校验。")
        return

    with open("last_generated_file.txt", "r") as f:
        target_file = f.read().strip()

    print(f"🕵️‍♂️ 启动 AI 质检员 | 目标: {target_file}")

    if not os.path.exists(target_file):
        print(f"❌ 目标文件不存在: {target_file}")
        return

    with open(target_file, 'r', encoding='utf-8') as f:
        data_json = json.load(f)

    questions = data_json['data']
    doubts_count = 0

    print(f"🚀 开始校验 {len(questions)} 道题目 (并发 {MAX_WORKERS})...")

    # 2. 并发执行校验
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_idx = {executor.submit(validate_single_question, q): i for i, q in enumerate(questions)}

        for future in tqdm(as_completed(future_to_idx), total=len(questions), unit="题"):
            idx = future_to_idx[future]
            try:
                is_doubt, reason = future.result()
                if is_doubt:
                    doubts_count += 1
                    # 将存疑标记插入到 analysis 字段的最前面
                    original_analysis = questions[idx].get('analysis', "")
                    questions[idx]['analysis'] = reason + original_analysis
            except Exception:
                pass

    # 3. 保存结果
    data_json['data'] = questions
    data_json['source'] += " + AI Validated"

    with open(target_file, 'w', encoding='utf-8') as f:
        json.dump(data_json, f, ensure_ascii=False, indent=2)

    print(f"✅ 质检完成！共标记 {doubts_count} 处存疑答案。")


if __name__ == "__main__":
    main()