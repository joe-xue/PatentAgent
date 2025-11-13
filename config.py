import os
from pathlib import Path
from dotenv import find_dotenv, set_key, load_dotenv
import prompts

# 加载 .env 文件中的环境变量
env_file = find_dotenv()
if not env_file:
    env_file = Path(".env")
    env_file.touch()
load_dotenv(env_file)

def load_config() -> dict:
    """加载配置，支持 openai兼容格式 / google 分节嵌套结构。"""
    return {
        "provider": os.getenv("PROVIDER", "openai"),
        "azure": {
            "api_base": os.getenv("AZURE_OPENAI_ENDPOINT", "https://api.mistral.ai/v1"),
            "api_key": os.getenv("AZURE_OPENAI_API_KEY", ""),
            "model": os.getenv("AZURE_OPENAI_DEPLOYMENT_NAME", "gpt-5"),
            "api_version": os.getenv("OPENAI_API_VERSION", "2025-01-01-preview"),
            "proxy_url": os.getenv("OPENAI_PROXY_URL", ""),
        },
        "openai": {
            "api_base": os.getenv("OPENAI_API_BASE", "https://api.mistral.ai/v1"),
            "api_key": os.getenv("OPENAI_API_KEY", ""),
            "model": os.getenv("OPENAI_MODEL_NAME", "mistral-medium-latest"),
            "proxy_url": os.getenv("OPENAI_PROXY_URL", ""),
        },
        "google": {
            "api_key": os.getenv("GOOGLE_API_KEY", ""),
            "model": os.getenv("GOOGLE_MODEL", "gemini-2.5-flash"),
            "proxy_url": os.getenv("GOOGLE_PROXY_URL", ""),
        },
    }

def save_config(cfg: dict):
    """将配置保存到 .env 文件。"""
    set_key(env_file, "PROVIDER", cfg.get("provider", "openai"))
    if "azure" in cfg:
        set_key(env_file, "AZURE_OPENAI_API_KEY", cfg["azure"].get("api_key", ""))
        set_key(env_file, "AZURE_OPENAI_ENDPOINT", cfg["azure"].get("api_base", ""))
        set_key(env_file, "AZURE_OPENAI_DEPLOYMENT_NAME", cfg["azure"].get("model", ""))
        set_key(env_file, "OPENAI_API_VERSION", cfg["azure"].get("api_version", ""))
        set_key(env_file, "OPENAI_PROXY_URL", cfg["azure"].get("proxy_url", ""))
    if "openai" in cfg:
        set_key(env_file, "OPENAI_API_KEY", cfg["openai"].get("api_key", ""))
        set_key(env_file, "OPENAI_API_BASE", cfg["openai"].get("api_base", ""))
        set_key(env_file, "OPENAI_MODEL_NAME", cfg["openai"].get("model", ""))
        set_key(env_file, "OPENAI_PROXY_URL", cfg["openai"].get("proxy_url", ""))
    if "google" in cfg:
        set_key(env_file, "GOOGLE_API_KEY", cfg["google"].get("api_key", ""))
        set_key(env_file, "GOOGLE_MODEL", cfg["google"].get("model", ""))
        set_key(env_file, "GOOGLE_PROXY_URL", cfg["google"].get("proxy_url", ""))


UI_SECTION_ORDER = ["title", "technical_field", "background", "invention", "figure_description", "implementation", "claims", "abstract", "drawings"]
UI_SECTION_CONFIG = {
    "title": {
        "label": "发明名称",
        "workflow_keys": ["title_options"],
        "dependencies": ["structured_brief"],
    },
    "technical_field": {
        "label": "技术领域",
        "workflow_keys": ["tech_field"],
        "dependencies": ["structured_brief"],
    },
    "background": {
        "label": "背景技术",
        "workflow_keys": ["background_problem", "background_context"],
        "dependencies": ["structured_brief"],
    },
    "invention": {
        "label": "发明内容",
        "workflow_keys": ["invention_purpose", "solution_points", "invention_solution_detail", "invention_effects"],
        "dependencies": ["background", "structured_brief"],
    },
    "figure_description": {
        "label": "附图说明",
        "workflow_keys": ["mermaid_ideas", "figure_description"],
        "dependencies": ["invention"],
    },
    "implementation": {
        "label": "具体实施方式",
        "workflow_keys": ["implementation_details"],
        "dependencies": ["invention", "structured_brief"],
    },
    "claims": {
        "label": "权利要求书",
        "workflow_keys": ["claims_text"],
        "dependencies": ["invention", "structured_brief"],
    },
    "abstract": {
        "label": "摘要",
        "workflow_keys": ["abstract_text"],
        "dependencies": ["invention", "structured_brief"],
    },
    "drawings": {
        "label": "附图",
        "workflow_keys": [],
        "dependencies": ["invention"],
    },
}

WORKFLOW_CONFIG = {
    # 发明名称
    "title_options": {
        "prompt": prompts.PROMPT_TITLE,
        "json_mode": True,
        "dependencies": ["core_inventive_concept", "technical_solution_summary"],
    },

    # 技术领域
    "tech_field": {
        "prompt": prompts.PROMPT_TECH_FIELD,
        "json_mode": False,
        "dependencies": ["core_inventive_concept", "technical_solution_summary"],
    },

    # 背景技术
    "background_problem": {
        "prompt": prompts.PROMPT_BACKGROUND_PROBLEM,
        "json_mode": False,
        "dependencies": ["problem_statement"],
    },
    "background_context": {
        "prompt": prompts.PROMPT_BACKGROUND_CONTEXT,
        "json_mode": False,
        "dependencies": ["background_technology", "background_problem"],
    },

    # 发明内容
    "invention_purpose": {
        "prompt": prompts.PROMPT_INVENTION_PURPOSE,
        "json_mode": False,
        "dependencies": ["background_problem"],
    },
    "solution_points": {
        "prompt": prompts.PROMPT_INVENTION_SOLUTION_POINTS,
        "json_mode": True,
        "dependencies": ["technical_solution_summary", "key_components_or_steps"],
    },
    "invention_solution_detail": {
        "prompt": prompts.PROMPT_INVENTION_SOLUTION_DETAIL,
        "json_mode": False,
        "dependencies": ["core_inventive_concept", "technical_solution_summary", "key_components_or_steps"],
    },
    "invention_effects": {
        "prompt": prompts.PROMPT_INVENTION_EFFECTS,
        "json_mode": False,
        "dependencies": ["solution_points", "achieved_effects"],
    },

    # 附图说明（先生成构思，再生成说明）
    "mermaid_ideas": {
        "prompt": prompts.PROMPT_MERMAID_IDEAS,
        "json_mode": True,
        "dependencies": ["invention_solution_detail"],
    },
    "figure_description": {
        "prompt": prompts.PROMPT_FIGURE_DESCRIPTION,
        "json_mode": False,
        "dependencies": ["mermaid_ideas"],
    },
    # 附图标号表与Mermaid代码供附图UI使用（不作为章节）
    "figure_labels": {
        "prompt": prompts.PROMPT_FIGURE_LABELS,
        "json_mode": True,
        "dependencies": ["key_components_or_steps"],
    },
    "mermaid_code": {
        "prompt": prompts.PROMPT_MERMAID_CODE,
        "json_mode": False,
        "dependencies": ["mermaid_ideas", "invention_solution_detail"],
    },

    # 具体实施方式
    "implementation_details": {
        "prompt": prompts.PROMPT_IMPLEMENTATION_POINT,
        "json_mode": False,
        "dependencies": ["solution_points"],
    },

    # 权利要求书
    "claims_text": {
        "prompt": prompts.PROMPT_CLAIMS,
        "json_mode": False,
        "dependencies": ["core_inventive_concept", "technical_solution_summary", "key_components_or_steps", "solution_points"],
    },
    # 可选的校验工作流（不作为章节直接展示）
    "claims_check": {
        "prompt": prompts.PROMPT_CLAIMS_CHECK,
        "json_mode": True,
        "dependencies": ["claims_text", "key_components_or_steps"],
    },

    # 摘要
    "abstract_text": {
        "prompt": prompts.PROMPT_ABSTRACT,
        "json_mode": False,
        "dependencies": ["problem_statement", "solution_points", "achieved_effects"],
    },

    # 全局重构与润色（用于预览阶段的总编，不直接归属某章节生成）
    "global_refine": {
        "prompt": prompts.PROMPT_GLOBAL_RESTRUCTURE_AND_POLISH,
        "json_mode": False,
        "dependencies": [],
    },
}
 