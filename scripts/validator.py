import json
import os
import time
from zhipuai import ZhipuAI
from concurrent.futures import ThreadPoolExecutor, as_completed
from tqdm import tqdm

# ================= 🛡️ 校验配置 =================
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY")
AI_MODEL_NAME = "glm-4-flash"
MAX_WORKERS = 20  # 校验速度极快，拉满
# ===============================================

client = ZhipuAI(api_key=ZHIPU_API_KEY)


def validate_single_question(question):
    """
    使用 AI 对单个题目进行逻辑/事实校验
    """
    # 构造清晰的校验上下文
    options_text = ""
    if question.get('options'):
        options_text = "\n".join([f"{opt['label']}. {opt['text']}" for opt in question['options']])

    # 针对性 Prompt
    prompt = f"""
    [任务]
    你是一个全学科试题审核专家。请检查以下题目的“参考答案”是否存在明显错误。

    [题目信息]
    - 学科背景: {question.get('chapter', '通用')}
    - 题型: {question.get('category', '未知')}
    - 题干: {question['content']}
    - 选项: 
    {options_text}

    [给出的参考答案]
    {question['answer']}

    [审核标准]
    1. **客观错误**：如 1+1=3、青霉素治疗病毒感冒等明显的事实/逻辑错误。
    2. **格式错误**：如多选题只选了一个，或者单选题选了ABC。
    3. **主观题宽容度**：简答/编程/论述题，只要答案言之有理，即视为正确。

    [输出指令]
    - 如果答案正确：仅回复 "CORRECT"。
    - 如果答案存疑：回复 "DOUBT: " + 简短的错误理由 + 你认为的正确答案。
    """

    try:
        response = client.chat.completions.create(
            model=AI_MODEL_NAME,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,  # 极低温度，保持客观
            max_tokens=200
        )
        result = response.choices[0].message.content.strip()

        if result.startswith("DOUBT") or "存疑" in result or "错误" in result:
            # 清洗前缀，提取理由
            reason = result.replace("DOUBT:", "").replace("DOUBT", "").strip()
            return True, f"【答案存疑】AI审核提示：{reason}\n\n"
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

    print(f"🕵️‍♂️ 启动 AI 质检员 | 目标文件: {target_file}")

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
            except Exception as e:
                # 校验失败不应该影响原数据，忽略即可
                pass

    # 3. 保存结果
    data_json['data'] = questions
    data_json['source'] += " + AI Validated"

    with open(target_file, 'w', encoding='utf-8') as f:
        json.dump(data_json, f, ensure_ascii=False, indent=2)

    print(f"✅ 质检完成！共标记 {doubts_count} 处存疑答案。")


if __name__ == "__main__":
    main()