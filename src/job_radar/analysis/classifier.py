from __future__ import annotations

from collections import Counter


CATEGORY_PATTERNS: dict[str, list[str]] = {
    "大模型应用/Agent": ["agent", "智能体", "rag", "大模型应用", "llm", "知识库", "mcp"],
    "算法/模型": ["算法", "机器学习", "深度学习", "nlp", "多模态", "微调", "训练"],
    "平台/基础设施": ["mlops", "推理优化", "vllm", "cuda", "模型部署", "ai infra", "算力"],
    "产品/解决方案": ["ai产品", "大模型产品", "解决方案", "售前", "实施", "交付"],
    "机器人/具身智能": ["机器人", "具身智能", "slam", "运动控制", "机器视觉"],
    "AI运营/评测/数据": ["ai运营", "模型评测", "数据标注", "数据治理", "ai测试"],
}

SKILL_PATTERNS = [
    "Python", "Java", "C++", "Go", "FastAPI", "Spring", "LangChain", "LangGraph",
    "Dify", "Coze", "RAG", "MCP", "Docker", "Kubernetes", "vLLM", "PyTorch",
    "TensorFlow", "CUDA", "Redis", "MySQL", "PostgreSQL", "Elasticsearch", "Milvus",
]


def classify(title: str, description: str) -> list[str]:
    text = f"{title}\n{description}".lower()
    result = []
    for category, keywords in CATEGORY_PATTERNS.items():
        if any(keyword.lower() in text for keyword in keywords):
            result.append(category)
    return result or ["其他/待复核"]


def extract_skills(text: str) -> list[str]:
    lower = (text or "").lower()
    return [skill for skill in SKILL_PATTERNS if skill.lower() in lower]


def counter(values: list[list[str]]) -> Counter[str]:
    result: Counter[str] = Counter()
    for group in values:
        result.update(group)
    return result
