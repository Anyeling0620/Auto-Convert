import json
import os
import time
from zhipuai import ZhipuAI
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

# ================= 配置加载 =================
CONFIG_FILE = "config.json"
DEFAULT_CONFIG = {"subject_name": "通用", "max_workers": 10}


def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except:
            pass
    return DEFAULT_CONFIG


APP_CONFIG = load_config()
SUBJECT = APP_CONFIG.get("subject_name", "通用")
# ===========================================

ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY")
AI_MODEL_NAME = "glm-4-flash"
MAX_WORKERS = 20  # 校验可以快一点

client = ZhipuAI(api_key=ZHIPU_API_KEY)


def validate_single_question(question):
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
    if not os.path.exists("last_generated_file.txt"):
        return

    with open("last_generated_file.txt", "r") as f:
        target_file = f.read().strip()

    print(f"🕵️‍♂️ 启动 [{SUBJECT}] 质检员 | 目标: {target_file}")

    if not os.path.exists(target_file): return

    with open(target_file, 'r', encoding='utf-8') as f:
        data_json = json.load(f)

    questions = data_json['data']
    doubts_count = 0

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        future_to_idx = {executor.submit(validate_single_question, q): i for i, q in enumerate(questions)}

        for future in tqdm(as_completed(future_to_idx), total=len(questions), unit="题"):
            idx = future_to_idx[future]
            try:
                is_doubt, reason = future.result()
                if is_doubt:
                    doubts_count += 1
                    original_analysis = questions[idx].get('analysis', "")
                    questions[idx]['analysis'] = reason + original_analysis
            except:
                pass

    data_json['data'] = questions
    data_json['source'] += " + AI Validated"

    with open(target_file, 'w', encoding='utf-8') as f:
        json.dump(data_json, f, ensure_ascii=False, indent=2)

    print(f"✅ 质检完成！共标记 {doubts_count} 处存疑答案。")


if __name__ == "__main__":
    main()