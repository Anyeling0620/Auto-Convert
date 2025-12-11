import json
import os
import uuid
import hashlib
from docx import Document
from zhipuai import ZhipuAI

# === 配置 ===
INPUT_DIR = "input"
OUTPUT_DIR = "output"
OUTPUT_FILE = "questions_full.json"
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY")

client = ZhipuAI(api_key=ZHIPU_API_KEY)


def read_docx(file_path):
    if not os.path.exists(file_path): return ""
    doc = Document(file_path)
    # 过滤掉空行，合并所有段落
    return "\n".join([p.text.strip() for p in doc.paragraphs if p.text.strip()])


def get_chunks(text, chunk_size=3000, overlap=500):
    """滑动窗口切分"""
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
    """生成指纹用于去重"""
    # 使用 题目内容 + 选项 作为唯一标识
    raw = q_obj.get("content", "") + str(q_obj.get("options", ""))
    return hashlib.md5(raw.encode('utf-8')).hexdigest()


def call_glm4(text_chunk):
    prompt = """
    你是一个通用试题提取助手。请将输入的文本片段转换为严格的 JSON 数组。

    【处理规则】
    1. 输入文本是切片，开头或结尾可能包含残缺的题目，**请直接忽略残缺部分**，只提取完整的。
    2. 自动推断题型 (type): SINGLE_CHOICE, MULTI_CHOICE, TRUE_FALSE, FILL_BLANK, ESSAY。
    3. 自动推断分类 (category): 如"选择题", "填空题"等。

    【输出格式】
    Strict JSON Array ONLY:
    [
      {
        "category": "string",
        "type": "string",
        "content": "题干内容",
        "options": [{"label":"A", "text":"选项内容"}], 
        "answer": "答案",
        "analysis": "解析(无则留空)"
      }
    ]
    """

    try:
        response = client.chat.completions.create(
            model="glm-4-flash",  # 使用 Flash 模型速度快且便宜
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text_chunk}
            ],
            temperature=0.1
        )
        content = response.choices[0].message.content
        # 清洗 Markdown
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        return json.loads(content.strip())
    except Exception as e:
        print(f"⚠️ Chunk parse error: {e}")
        return []


def main():
    # 1. 寻找 input 目录下的 docx 文件
    docx_files = [f for f in os.listdir(INPUT_DIR) if f.endswith(".docx")]
    if not docx_files:
        print("❌ No .docx found in input/")
        return

    target_file = os.path.join(INPUT_DIR, docx_files[0])  # 只处理第一个找到的
    print(f"🚀 Processing: {target_file}")

    raw_text = read_docx(target_file)
    chunks = get_chunks(raw_text)

    all_questions = []
    seen_hashes = set()

    print(f"📂 Split into {len(chunks)} chunks. Starting AI processing...")

    for i, chunk in enumerate(chunks):
        print(f"   ⚡ Processing chunk {i + 1}/{len(chunks)}...")
        items = call_glm4(chunk)

        new_count = 0
        for item in items:
            fp = generate_fingerprint(item)
            if fp in seen_hashes: continue  # 去重

            seen_hashes.add(fp)
            # 补全字段
            item['id'] = str(uuid.uuid4())
            item['number'] = len(all_questions) + 1
            if 'chapter' not in item: item['chapter'] = "导入题目"

            all_questions.append(item)
            new_count += 1
        print(f"      -> Extracted {new_count} new questions.")

    # 保存结果
    final_json = {
        "version": "GLM4-Auto",
        "source": docx_files[0],
        "total_count": len(all_questions),
        "data": all_questions
    }

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)

    print(f"✅ Success! Saved to {out_path}")


if __name__ == "__main__":
    main()