import json
import os
import uuid
import hashlib
from docx import Document
from zhipuai import ZhipuAI

# === 配置区域 ===
INPUT_DIR = "input"
OUTPUT_DIR = "output"
OUTPUT_FILE = "questions_full.json"
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY")

client = ZhipuAI(api_key=ZHIPU_API_KEY)

# 标准分类白名单
STANDARD_CATEGORIES = {
    "单选题", "多选题", "判断题", "填空题", "简答题", 
    "名词解释题", "案例分析题", "计算题", "证明题", "配伍题"
}

def read_docx(file_path):
    if not os.path.exists(file_path): return ""
    doc = Document(file_path)
    return "\n".join([p.text.strip() for p in doc.paragraphs if p.text.strip()])

def get_chunks(text, chunk_size=1500, overlap=200):
    """切片函数"""
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
    raw = q_obj.get("content", "") + str(q_obj.get("options", ""))
    return hashlib.md5(raw.encode('utf-8')).hexdigest()

def normalize_category(raw_cat):
    if not raw_cat: return "综合题"
    cat = raw_cat.strip()
    if "多选" in cat or "不定项" in cat: return "多选题"
    if "单选" in cat: return "单选题"
    if "判断" in cat or "是非" in cat: return "判断题"
    if "填空" in cat: return "填空题"
    if "名词" in cat: return "名词解释题"
    if "简答" in cat or "问答" in cat or "论述" in cat: return "简答题"
    if cat in STANDARD_CATEGORIES: return cat
    if not cat.endswith("题"): return cat + "题"
    return cat

# === [核心新增] 第一步：提取全局答案 ===
def extract_global_answers(full_text):
    """
    让 AI 通读全文，只提取答案部分。
    GLM-4-Flash 支持 128k 上下文，读整个文档没问题。
    """
    print("   🔍 Scanning document for Answer Key...")
    prompt = """
    你是一个文档分析助手。请阅读下面的文档全文，提取出其中的“答案”部分。
    
    【要求】
    1. 如果文档包含集中的“答案页”或“参考答案”部分，请将这部分内容原样提取出来。
    2. 如果答案分散在题目后，请提取出所有能找到的答案信息。
    3. 如果找不到答案，返回"无答案"。
    4. **只返回答案文本**，不要包含题目内容，不要废话。
    """
    
    try:
        # 截取前 60000 字符（防止极端超长，一般文档足够了）
        safe_text = full_text[:60000] 
        response = client.chat.completions.create(
            model="glm-4-flash",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": safe_text}
            ],
            temperature=0.1
        )
        answers = response.choices[0].message.content
        print(f"   ✅ Answer Key extracted (Length: {len(answers)} chars)")
        return answers
    except Exception as e:
        print(f"   ⚠️ Failed to extract answers: {e}")
        return ""

# === [修改] 第二步：携带答案提取题目 ===
def call_glm4_with_answers(text_chunk, answer_key):
    prompt = f"""
    你是一个通用试题提取助手。请将输入的文本片段转换为严格的 JSON 数组。
    
    【参考答案库】
    这是本文档的答案部分，请根据题目编号或内容，尝试为下面的题目匹配答案：
    ----------------
    {answer_key[:5000]} 
    (如果答案太长已截断，请尽力匹配)
    ----------------
    
    【核心任务】
    1. 提取文本片段中的完整题目。
    2. **自动配对答案**：利用上面的参考答案库填入 `answer` 字段。如果找不到匹配答案，留空。
    3. 忽略切片开头结尾的不完整句子。
    
    【输出格式】
    Strict JSON Array ONLY:
    [
      {{
        "category": "单选题",
        "type": "SINGLE_CHOICE",
        "content": "题干",
        "options": [{{"label":"A", "text":"..."}}], 
        "answer": "A",
        "analysis": ""
      }}
    ]
    """
    
    try:
        response = client.chat.completions.create(
            model="glm-4-flash",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text_chunk}
            ],
            temperature=0.1,
            top_p=0.7,
            max_tokens=4000
        )
        content = response.choices[0].message.content
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]
            
        return json.loads(content.strip())
    except Exception as e:
        print(f"   ⚠️ Chunk parse error: {e}")
        return []

def main():
    docx_files = [f for f in os.listdir(INPUT_DIR) if f.endswith(".docx")]
    if not docx_files:
        print("❌ No .docx files found.")
        return
    
    all_questions = []
    seen_hashes = set()
    
    for filename in docx_files:
        file_path = os.path.join(INPUT_DIR, filename)
        print(f"\n🚀 Processing: {filename}")
        
        raw_text = read_docx(file_path)
        if not raw_text: continue

        # 1. 先提取全局答案 (利用 GLM-4 长上下文)
        global_answers = extract_global_answers(raw_text)

        # 2. 再切片提取题目 (把答案传进去)
        chunks = get_chunks(raw_text, chunk_size=1500, overlap=300)
        print(f"   📂 Split into {len(chunks)} chunks.")
        
        for i, chunk in enumerate(chunks):
            print(f"   ⚡ Analyzing chunk {i+1}/{len(chunks)}...")
            
            # 调用带答案的提取函数
            items = call_glm4_with_answers(chunk, global_answers)
            
            new_count = 0
            for item in items:
                fp = generate_fingerprint(item)
                if fp in seen_hashes: continue
                seen_hashes.add(fp)
                
                item['category'] = normalize_category(item.get('category', '综合题'))
                item['id'] = str(uuid.uuid4())
                item['number'] = len(all_questions) + 1
                item['chapter'] = filename.replace(".docx", "")
                
                all_questions.append(item)
                new_count += 1
            print(f"      -> Added {new_count} questions.")

    final_json = {
        "version": "Universal-V3-WithAnswers",
        "source": "Smart Import",
        "total_count": len(all_questions),
        "data": all_questions
    }
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)
    print(f"\n✅ All Done! Saved to: {out_path}")

if __name__ == "__main__":
    main()