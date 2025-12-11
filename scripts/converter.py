import json
import os
import uuid
import hashlib
import re
from docx import Document
from zhipuai import ZhipuAI

# === 配置区域 ===
INPUT_DIR = "input"
OUTPUT_DIR = "output"
OUTPUT_FILE = "questions_full.json"
ZHIPU_API_KEY = os.getenv("ZHIPU_API_KEY")

client = ZhipuAI(api_key=ZHIPU_API_KEY)

# === 标准分类白名单 (这是你希望APP里出现的标准叫法) ===
STANDARD_CATEGORIES = {
    "单选题", "多选题", "判断题", "填空题", "简答题", 
    "名词解释题", "案例分析题", "计算题", "证明题", "配伍题"
}

def read_docx(file_path):
    if not os.path.exists(file_path): return ""
    doc = Document(file_path)
    # 过滤空行，保留段落结构，用换行符连接
    return "\n".join([p.text.strip() for p in doc.paragraphs if p.text.strip()])

def get_chunks(text, chunk_size=3000, overlap=500):
    """滑动窗口切分，保证长题目不被截断"""
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
    raw = q_obj.get("content", "") + str(q_obj.get("options", ""))
    return hashlib.md5(raw.encode('utf-8')).hexdigest()

def normalize_category(raw_cat):
    """
    【核心逻辑】强制归一化分类名称
    不管AI输出什么，只要包含关键字，就强制映射到标准词。
    """
    if not raw_cat: return "综合题"
    
    cat = raw_cat.strip()
    
    # 1. 关键字强制映射 (优先级从高到低)
    if "多选" in cat or "不定项" in cat: return "多选题"
    if "单选" in cat: return "单选题"
    if "判断" in cat or "是非" in cat: return "判断题"
    if "填空" in cat: return "填空题"
    if "名词" in cat: return "名词解释题"
    if "计算" in cat: return "计算题"
    if "证明" in cat: return "证明题"
    if "案例" in cat or "病例" in cat: return "案例分析题"
    if "配伍" in cat or "连线" in cat: return "配伍题"
    if "简答" in cat or "问答" in cat or "论述" in cat: return "简答题"
    
    # 2. 如果没命中标准词，但已经在白名单里，直接返回
    if cat in STANDARD_CATEGORIES:
        return cat
        
    # 3. 兜底规则：如果AI创造了新词（比如"作图"），强制加上后缀"题"
    if not cat.endswith("题"):
        return cat + "题"
        
    return cat

def call_glm4(text_chunk):
    # Prompt 优化：强调通用性和标准命名
    prompt = """
    你是一个通用试题结构化助手。请识别输入文本中的题目，并转换为 JSON 数组。
    
    【处理原则】
    1. **通用性**：不要预设学科，根据题目内容和选项特征自动推断。
    2. **标准命名**：category 字段请优先使用以下标准名称：
       - "单选题", "多选题", "判断题", "填空题", "简答题", "名词解释题", "计算题"
       - 只有当题目完全不符合上述类型时，才可以使用其他名称（如"作图题"）。
    3. **完整性**：忽略切片开头和结尾的残缺题目。

    【输出格式】
    Strict JSON Array ONLY:
    [
      {
        "category": "String (优先标准词)",
        "type": "SINGLE_CHOICE | MULTI_CHOICE | TRUE_FALSE | FILL_BLANK | ESSAY",
        "content": "题干内容",
        "options": [{"label":"A", "text":"..."}], 
        "answer": "参考答案",
        "analysis": "解析 (无则留空)"
      }
    ]
    """
    
    try:
        response = client.chat.completions.create(
            model="glm-4-flash",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text_chunk}
            ],
            temperature=0.1, # 低温，减少AI胡编乱造
            top_p=0.7
        )
        content = response.choices[0].message.content
        # 清洗可能存在的 Markdown 标记
        if "```json" in content:
            content = content.split("```json")[1].split("```")[0]
        elif "```" in content:
            content = content.split("```")[1].split("```")[0]
            
        return json.loads(content.strip())
    except Exception as e:
        print(f"⚠️ Chunk parse warning: {e}")
        return []

def main():
    # 扫描 input 文件夹
    docx_files = [f for f in os.listdir(INPUT_DIR) if f.endswith(".docx")]
    if not docx_files:
        print("❌ No .docx files found in input/ directory.")
        return
    
    # 只处理第一个文件，或者你可以改成循环处理所有
    target_file = os.path.join(INPUT_DIR, docx_files[0])
    print(f"🚀 Processing: {target_file}")
    
    raw_text = read_docx(target_file)
    if not raw_text:
        print("❌ File is empty or could not be read.")
        return

    chunks = get_chunks(raw_text)
    print(f"📂 Split document into {len(chunks)} chunks.")
    
    all_questions = []
    seen_hashes = set()
    
    for i, chunk in enumerate(chunks):
        print(f"⚡ Analyzing chunk {i+1}/{len(chunks)}...")
        items = call_glm4(chunk)
        
        new_count = 0
        for item in items:
            # 生成指纹
            fp = generate_fingerprint(item)
            if fp in seen_hashes: continue # 跳过重复
            
            seen_hashes.add(fp)
            
            # === 关键步骤：强制归一化 ===
            # 这里调用清洗函数，确保 "判断" -> "判断题"
            raw_cat = item.get('category', '综合题')
            item['category'] = normalize_category(raw_cat)
            
            # 补全其他字段
            item['id'] = str(uuid.uuid4())
            item['number'] = len(all_questions) + 1
            if 'chapter' not in item: 
                # 这里可以简单写死，或者让 AI 提取。为了通用性，写 "导入题库" 比较安全
                item['chapter'] = "导入题库" 
            
            all_questions.append(item)
            new_count += 1
            
        print(f"   -> Added {new_count} questions.")

    # 结果保存
    final_json = {
        "version": "Universal-V1",
        "source": docx_files[0],
        "total_count": len(all_questions),
        "data": all_questions
    }
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    out_path = os.path.join(OUTPUT_DIR, OUTPUT_FILE)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump(final_json, f, ensure_ascii=False, indent=2)
        
    print(f"✅ Conversion Complete! Saved to: {out_path}")

if __name__ == "__main__":
    main()