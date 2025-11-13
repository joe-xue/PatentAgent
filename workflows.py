import streamlit as st
import json
import time
import os
from datetime import datetime
from typing import List, Dict, Any, Optional
import prompts
from llm_client import LLMClient
from state_manager import get_active_content
from config import UI_SECTION_CONFIG, WORKFLOW_CONFIG, UI_SECTION_ORDER
from ui_components import clean_mermaid_code

# -------------- 行为与日志配置 --------------

# 默认跳过附图（文生图/图形生成）相关步骤
SKIP_DRAWINGS_DEFAULT = True

# 日志相关常量
LOG_ENABLED = True
LOG_DIR = "logs"
ARTIFACTS_DIR = os.path.join(LOG_DIR, "artifacts")
LOG_MAX_PROMPT_CHARS = 3000
LOG_MAX_CONTENT_CHARS = 5000
LOG_CAPTURE_FULL_PROMPT = True
LOG_CAPTURE_FULL_RESPONSE = True

# -------------- 工具函数 --------------

def safe_format_prompt(template: str, **kwargs) -> str:
    # 安全模板格式化，避免花括号导致 KeyError
    escaped = template.replace("{", "{{").replace("}", "}}")
    for k in kwargs.keys():
        escaped = escaped.replace(f"{{{{{k}}}}}", f"{{{k}}}")
    return escaped.format(**kwargs)

def ensure_version_state(key: str):
    if f"{key}_versions" not in st.session_state:
        st.session_state[f"{key}_versions"] = []
    if f"{key}_active_index" not in st.session_state:
        st.session_state[f"{key}_active_index"] = 0
    if "data_timestamps" not in st.session_state:
        st.session_state.data_timestamps = {}

def _truncate_text(text: Any, max_len: int) -> str:
    if text is None:
        return ""
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:
            text = repr(text)
    return text if len(text) <= max_len else text[:max_len] + f"...(truncated {len(text)-max_len} chars)"

def ensure_log_setup():
    if "log_file" not in st.session_state:
        os.makedirs(LOG_DIR, exist_ok=True)
        os.makedirs(ARTIFACTS_DIR, exist_ok=True)
        session_id = st.session_state.get("session_id")
        if not session_id:
            session_id = datetime.now().strftime("%Y%m%d_%H%M%S")
            st.session_state.session_id = session_id
        log_file = os.path.join(LOG_DIR, f"run_{session_id}.log")
        st.session_state.log_file = log_file
        # 为本次运行创建独立 artifacts 子目录
        run_artifacts_dir = os.path.join(ARTIFACTS_DIR, f"run_{session_id}")
        os.makedirs(run_artifacts_dir, exist_ok=True)
        st.session_state.run_artifacts_dir = run_artifacts_dir
    if "step_counter" not in st.session_state:
        st.session_state.step_counter = 0

def write_log(level: str, action: str, message: str, context: Optional[Dict[str, Any]] = None):
    if not LOG_ENABLED:
        return
    ensure_log_setup()
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "level": level,
        "action": action,
        "message": message,
    }
    if context:
        try:
            record["context"] = context
        except Exception:
            record["context"] = str(context)
    try:
        with open(st.session_state.log_file, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        st.warning(f"写入日志失败: {e}")

def render_logs_viewer():
    os.makedirs(LOG_DIR, exist_ok=True)
    st.subheader("执行日志")
    files = sorted([f for f in os.listdir(LOG_DIR) if f.endswith(".log")])
    default_index = None
    current_log = os.path.basename(st.session_state.get("log_file", "")) if "log_file" in st.session_state else None
    if current_log and current_log in files:
        default_index = files.index(current_log)
    selected = st.selectbox("选择日志文件", options=files, index=default_index if default_index is not None else (0 if files else None))
    if selected:
        path = os.path.join(LOG_DIR, selected)
        try:
            with open(path, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except Exception as e:
            st.error(f"读取日志失败: {e}")
            return
        if not lines:
            st.info("日志文件为空。")
            return
        count = st.number_input("显示最近行数", min_value=10, max_value=5000, value=min(200, len(lines)))
        start = max(0, len(lines) - int(count))
        st.text("".join(lines[start:]))

        # 列出对应 artifacts 目录
        run_id = selected.replace("run_", "").replace(".log", "")
        run_art_dir = os.path.join(ARTIFACTS_DIR, f"run_{run_id}")
        if os.path.isdir(run_art_dir):
            st.info(f"Artifacts 目录: {run_art_dir}")
            artifacts = sorted(os.listdir(run_art_dir))
            st.write("\n".join(artifacts[:50]) if artifacts else "（无 artifacts 文件）")

        try:
            with open(path, "rb") as fb:
                st.download_button("下载选定日志文件", data=fb.read(), file_name=selected)
        except Exception:
            pass

def _write_artifact(step_id: str, kind: str, content: str) -> str:
    """
    将完整的 prompt 或 response 写入 artifacts 文件。
    kind 取值：'prompt' 或 'response'
    返回写入的文件路径。
    """
    ensure_log_setup()
    filename = f"{step_id}_{kind}.txt"
    filepath = os.path.join(st.session_state.run_artifacts_dir, filename)
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            f.write(content or "")
    except Exception as e:
        write_log("WARN", "artifact:write_failed", f"写入artifact失败: {e}", {"step_id": step_id, "kind": kind})
    return filepath

def _messages_to_text(messages: List[Dict[str, str]]) -> str:
    """
    将 messages 转成纯文本，便于记录与存档。
    """
    parts = []
    for m in messages:
        role = m.get("role", "user")
        content = m.get("content", "")
        parts.append(f"[{role}] {content}")
    return "\n---\n".join(parts)

def call_llm(llm_client: LLMClient, messages: List[Dict[str, str]], json_mode: bool = False, tag: str = "llm_call", extra_ctx: Optional[Dict[str, Any]] = None) -> str:
    """
    统一封装对 LLM 的调用：
    - 记录请求与响应日志（片段与完整 artifacts）
    - 记录耗时、json_mode、tag
    - 返回模型原始字符串响应
    """
    ensure_log_setup()
    st.session_state.step_counter += 1
    step_id = f"{st.session_state.step_counter:04d}_{tag}"

    prompt_text = _messages_to_text(messages)
    prompt_snippet = _truncate_text(prompt_text, LOG_MAX_PROMPT_CHARS)
    prompt_art_path = ""
    if LOG_CAPTURE_FULL_PROMPT:
        prompt_art_path = _write_artifact(step_id, "prompt", prompt_text)

    ctx_req = {"step_id": step_id, "json_mode": json_mode, "tag": tag, "prompt_snippet": prompt_snippet}
    if prompt_art_path:
        ctx_req["prompt_artifact"] = prompt_art_path
    if extra_ctx:
        ctx_req.update(extra_ctx)

    write_log("DEBUG", "LLM:request", "发送给模型的输入", ctx_req)

    t0 = time.perf_counter()
    try:
        response_str = llm_client.call(messages, json_mode=json_mode)
    except Exception as e:
        t1 = time.perf_counter()
        write_log("ERROR", "LLM:call_failed", "模型调用失败", {"step_id": step_id, "error": str(e), "elapsed_s": round(t1 - t0, 3)})
        raise
    t1 = time.perf_counter()

    response_snippet = _truncate_text(response_str, LOG_MAX_CONTENT_CHARS)
    response_art_path = ""
    if LOG_CAPTURE_FULL_RESPONSE:
        response_art_path = _write_artifact(step_id, "response", response_str)

    ctx_resp = {
        "step_id": step_id,
        "json_mode": json_mode,
        "tag": tag,
        "elapsed_s": round(t1 - t0, 3),
        "response_len": len(response_str or ""),
        "response_snippet": response_snippet,
    }
    if response_art_path:
        ctx_resp["response_artifact"] = response_art_path
    write_log("INFO", "LLM:response", "模型返回内容", ctx_resp)

    return response_str

# -------------- 标题与附图构思规范化 --------------

def normalize_title_options(raw) -> List[str]:
    """
    将模型返回的 title_options 规范化为字符串列表。
    兼容：
    - list[str]
    - list[dict]（优先取 'title','name','text','value' 字段）
    - dict，包含 'titles','options','data','items','list' 等字段
    """
    def from_list(lst):
        titles = []
        for item in lst:
            if isinstance(item, str):
                titles.append(item.strip())
            elif isinstance(item, dict):
                for k in ['title', 'name', 'text', 'value']:
                    v = item.get(k)
                    if isinstance(v, str) and v.strip():
                        titles.append(v.strip())
                        break
        return titles

    if isinstance(raw, list):
        return from_list(raw)
    if isinstance(raw, dict):
        for key in ['titles', 'options', 'data', 'items', 'list', 'names']:
            v = raw.get(key)
            if isinstance(v, list):
                return from_list(v)
        for k in ['title', 'name', 'text', 'value']:
            v = raw.get(k)
            if isinstance(v, str) and v.strip():
                return [v.strip()]
    return []

def dedup_and_clean_titles(titles: List[str]) -> List[str]:
    seen = set()
    cleaned = []
    for t in titles:
        if not isinstance(t, str):
            continue
        s = t.strip()
        if not s:
            continue
        if s.lower() in {"data", "title", "titles", "options", "items", "list"}:
            continue
        if s not in seen:
            seen.add(s)
            cleaned.append(s)
    return cleaned

def normalize_ideas_container(raw) -> List[Dict[str, Any]]:
    """
    将模型返回的附图构思规范化为列表[list[dict]]，兼容：
    - list[dict]，每项包含 title/description
    - dict，包含 'items','data','list','ideas','options' 等列表字段
    """
    def from_list(lst):
        ideas: List[Dict[str, Any]] = []
        for item in lst:
            if isinstance(item, dict):
                title = item.get("title") or item.get("name") or item.get("text") or ""
                desc = item.get("description") or item.get("desc") or item.get("detail") or ""
                ideas.append({"title": title, "description": desc})
            elif isinstance(item, str) and item.strip():
                ideas.append({"title": item.strip(), "description": ""})
        return ideas

    if isinstance(raw, list):
        return from_list(raw)
    if isinstance(raw, dict):
        for key in ["items", "data", "list", "ideas", "options"]:
            v = raw.get(key)
            if isinstance(v, list):
                return from_list(v)
    return []

# -------------- Prompt 参数构建 --------------

def build_format_args(dependencies: List[str]) -> Dict[str, Any]:
    """
    根据依赖项列表，构建用于格式化Prompt的字典。
    """
    brief = st.session_state.get('structured_brief', {}) or {}
    format_args: Dict[str, Any] = {}

    for k in [
        "background_technology",
        "problem_statement",
        "core_inventive_concept",
        "technical_solution_summary",
        "achieved_effects",
    ]:
        format_args[k] = brief.get(k) or ""

    dep_used: Dict[str, Any] = {}
    for dep in dependencies:
        dep_content = get_active_content(dep)
        if dep_content is not None:
            format_args[dep] = dep_content
            dep_used[dep] = dep_content
        else:
            format_args[dep] = brief.get(dep)
            dep_used[dep] = brief.get(dep)

    components = brief.get('key_components_or_steps', [])
    text_lines: List[str] = []
    components_json_str = "[]"

    if isinstance(components, list):
        if components and isinstance(components[0], dict):
            text_lines = [
                f"{x.get('name','')}: {x.get('function','')}"
                for x in components
                if isinstance(x, dict)
            ]
            components_json_str = json.dumps(components, ensure_ascii=False)
        else:
            text_lines = [str(x) for x in components if x is not None]
            try:
                components_json_str = json.dumps(components, ensure_ascii=False)
            except Exception:
                components_json_str = json.dumps([str(x) for x in components if x is not None], ensure_ascii=False)
    elif isinstance(components, str):
        text_lines = [components]
        try:
            parsed = json.loads(components)
            components_json_str = json.dumps(parsed, ensure_ascii=False)
        except Exception:
            components_json_str = json.dumps([components], ensure_ascii=False)
    else:
        text_lines = []
        components_json_str = "[]"

    format_args["key_components_or_steps"] = "\n".join([line for line in text_lines if line])
    format_args["key_components_or_steps_json"] = components_json_str

    solution_points = get_active_content("solution_points") or []
    format_args["solution_points_str"] = "\n".join([f"{i+1}. {p}" for i, p in enumerate(solution_points)])

    write_log(
        "DEBUG",
        "build_format_args",
        "构建Prompt参数",
        {
            "dependencies": dependencies,
            "keys_provided": list(format_args.keys()),
            "solution_points_count": len(solution_points),
            "components_text_len": len(format_args.get("key_components_or_steps", "")),
            "deps_used_snippet": _truncate_text(json.dumps(dep_used, ensure_ascii=False), LOG_MAX_CONTENT_CHARS),
        }
    )
    return format_args

# -------------- 附图生成（可跳过） --------------

def generate_all_drawings(llm_client: LLMClient, invention_solution_detail: str):
    """
    统一生成所有附图：先构思，然后为每个构思生成代码。
    可通过 st.session_state['skip_drawings'] 或 SKIP_DRAWINGS_DEFAULT 跳过。
    """
    skip_drawings = st.session_state.get("skip_drawings", SKIP_DRAWINGS_DEFAULT)
    if skip_drawings:
        write_log("INFO", "drawings:skip", "已配置为跳过附图生成")
        ensure_version_state("drawings")
        st.session_state["drawings_versions"].append([])
        st.session_state["drawings_active_index"] = len(st.session_state["drawings_versions"]) - 1
        st.session_state.data_timestamps['drawings'] = time.time()
        return

    write_log("INFO", "drawings:start", "开始生成附图", {"has_solution_detail": bool(invention_solution_detail)})
    if not invention_solution_detail:
        st.warning("无法生成附图，因为“发明内容”>“技术解决方案”内容为空。")
        write_log("WARN", "drawings:abort", "技术解决方案为空，附图生成终止")
        return

    ideas_prompt = safe_format_prompt(
        prompts.PROMPT_MERMAID_IDEAS,
        invention_solution_detail=invention_solution_detail
    )
    ideas_response_str = call_llm(
        llm_client,
        messages=[{"role": "user", "content": ideas_prompt}],
        json_mode=True,
        tag="drawings_ideas",
        extra_ctx={"section": "drawings"}
    )
    try:
        ideas_raw = json.loads(ideas_response_str.strip())
    except json.JSONDecodeError:
        st.error(f"附图构思返回格式错误，期望列表或包含列表的对象，但得到: {ideas_response_str}")
        write_log("ERROR", "drawings:ideas_parse_error", "构思JSON解析失败", {"raw_snippet": _truncate_text(ideas_response_str, LOG_MAX_CONTENT_CHARS)})
        return

    ideas = normalize_ideas_container(ideas_raw)
    if not ideas:
        st.error("附图构思列表为空或不可解析，请重试。")
        write_log("ERROR", "drawings:ideas_empty", "规范化后附图构思为空", {"normalized_len": 0})
        return

    drawings = []
    progress_bar = st.progress(0, text="正在生成附图代码...")
    for i, idea in enumerate(ideas):
        idea_title = idea.get('title') or f'附图构思 {i+1}'
        idea_desc = idea.get('description') or ''
      
        code_prompt = safe_format_prompt(
            prompts.PROMPT_MERMAID_CODE,
            title=idea_title,
            description=idea_desc,
            invention_solution_detail=invention_solution_detail
        )
        code = call_llm(
            llm_client,
            messages=[{"role": "user", "content": code_prompt}],
            json_mode=False,
            tag=f"drawings_code_{i+1}",
            extra_ctx={"idea_title": idea_title}
        )
        cleaned_code = clean_mermaid_code(code)
        write_log("INFO", "drawings:code_generated", "附图代码生成完成", {"index": i, "title": idea_title, "code_len": len(code), "cleaned_len": len(cleaned_code)})

        drawings.append({
            "title": idea_title,
            "description": idea_desc,
            "code": cleaned_code
        })
        progress_bar.progress((i + 1) / len(ideas), text=f"已生成附图: {idea_title}")
  
    ensure_version_state("drawings")
    st.session_state.drawings_versions.append(drawings)
    st.session_state.drawings_active_index = len(st.session_state.drawings_versions) - 1
    st.session_state.data_timestamps['drawings'] = time.time()
    write_log("INFO", "drawings:done", "附图生成完成并保存版本", {"versions_count": len(st.session_state.drawings_versions)})

# -------------- 章节内容兜底构造 --------------

def _fallback_technical_field(brief: Dict[str, Any]) -> str:
    tech = (brief.get("background_technology") or "").strip()
    core = (brief.get("core_inventive_concept") or "").strip()
    if tech and core:
        return f"本发明涉及{tech}领域，尤其涉及基于{core}的相关技术。"
    if tech:
        return f"本发明涉及{tech}领域，尤其涉及相关系统与方法。"
    if core:
        return f"本发明涉及相关技术领域，尤其涉及基于{core}的系统与方法。"
    return "本发明涉及相关技术领域。"

def _fallback_drawings_desc() -> str:
    return "（本申请无附图）"

def _fallback_claims(brief: Dict[str, Any]) -> str:
    core = (brief.get("core_inventive_concept") or "").strip()
    sol = (brief.get("technical_solution_summary") or "").strip()
    eff = (brief.get("achieved_effects") or "").strip()
    points = st.session_state.get("solution_points") or []
    p1 = points[0] if points else core or "所述技术方案"
    claim1 = f"1. 一种系统，其特征在于，所述系统包括感知模块、处理与控制模块以及显示模块，所述处理与控制模块用于执行{p1}，从而实现{eff or '预期技术效果'}。"
    claim2 = f"2. 根据权利要求1所述的系统，其特征在于，所述处理与控制模块被配置为依据环境状态与用户偏好对显示内容与参数进行自适应调整。"
    claim3 = f"3. 一种方法，其特征在于，包括：采集环境与用户相关数据；基于所述数据进行{core or '关键处理'}；并根据{sol or '所述处理结果'}输出显示控制与交互响应。"
    return "\n".join([claim1, claim2, claim3])

def _fallback_abstract(brief: Dict[str, Any]) -> str:
    tech = (brief.get("background_technology") or "").strip()
    core = (brief.get("core_inventive_concept") or "").strip()
    sol = (brief.get("technical_solution_summary") or "").strip()
    eff = (brief.get("achieved_effects") or "").strip()
    parts = []
    if tech:
        parts.append(f"本发明涉及{tech}领域")
    else:
        parts.append("本发明涉及相关技术领域")
    if core:
        parts.append(f"提出一种基于{core}的技术方案")
    if sol:
        parts.append(f"通过所述方案{sol}")
    if eff:
        parts.append(f"能够实现{eff}")
    return "，".join(parts) + "。"

def _stringify_block(block: Any) -> str:
    if block is None:
        return ""
    if isinstance(block, str):
        return block.strip()
    if isinstance(block, list):
        items = []
        for i, it in enumerate(block):
            if isinstance(it, (dict, list)):
                try:
                    items.append(json.dumps(it, ensure_ascii=False))
                except Exception:
                    items.append(str(it))
            else:
                items.append(str(it))
        return "\n".join(items).strip()
    if isinstance(block, dict):
        try:
            return json.dumps(block, ensure_ascii=False, indent=2)
        except Exception:
            return str(block)
    return str(block)

# -------------- UI章节生成与组装 --------------

def generate_ui_section(llm_client: LLMClient, ui_key: str):
    """为单个UI章节执行生成流程（含日志、容错与兜底）。"""
    if "skip_drawings" not in st.session_state:
        st.session_state.skip_drawings = SKIP_DRAWINGS_DEFAULT

    write_log("INFO", "ui_section:start", f"开始生成章节: {ui_key}", {"ui_key": ui_key})

    # 附图类章节：根据配置跳过
    if ui_key in ("drawings", "figures", "drawings_description", "figures_description", "figures_desc"):
        skip_drawings = st.session_state.get("skip_drawings", SKIP_DRAWINGS_DEFAULT)
        if ui_key == "drawings":
            invention_solution_detail = get_active_content("invention_solution_detail")
            generate_all_drawings(llm_client, invention_solution_detail)
            write_log("INFO", "ui_section:done", "附图章节处理完成", {"ui_key": ui_key, "skipped": skip_drawings})
            return
        else:
            if skip_drawings:
                content = _fallback_drawings_desc()
                ensure_version_state(ui_key)
                st.session_state[f"{ui_key}_versions"].append(content)
                st.session_state[f"{ui_key}_active_index"] = len(st.session_state[f"{ui_key}_versions"]) - 1
                st.session_state.data_timestamps[ui_key] = time.time()
                write_log("INFO", "ui_section:drawings_desc_placeholder", "附图说明采用无附图占位", {"ui_key": ui_key})
                return

    # --- 步骤 1: 生成所有微观组件（每步记录输入与输出） ---
    workflow_keys = UI_SECTION_CONFIG[ui_key]["workflow_keys"]
    write_log("DEBUG", "ui_section:workflow_keys", "章节工作流组件", {"ui_key": ui_key, "workflow_keys": workflow_keys})

    for micro_key in workflow_keys:
        step_config = WORKFLOW_CONFIG[micro_key]
        format_args = build_format_args(step_config["dependencies"])

        if micro_key == "implementation_details":
            points = get_active_content("solution_points") or []
            details = []
            write_log("DEBUG", "ui_section:impl_details:start", "开始逐点生成实施例细节", {"points_count": len(points)})
            for i, point in enumerate(points):
                point_prompt = safe_format_prompt(step_config["prompt"], point=point)
                detail = call_llm(
                    llm_client,
                    messages=[{"role": "user", "content": point_prompt}],
                    json_mode=False,
                    tag=f"implementation_detail_{i+1}",
                    extra_ctx={"micro_key": micro_key}
                )
                details.append(detail)
            ensure_version_state(micro_key)
            st.session_state[f"{micro_key}_versions"].append(details)
            st.session_state[f"{micro_key}_active_index"] = len(st.session_state[f"{micro_key}_versions"]) - 1
            st.session_state.data_timestamps[micro_key] = time.time()
            write_log("INFO", "ui_section:impl_details:done", "实施例细节生成完成并保存版本", {"versions_count": len(st.session_state[f"{micro_key}_versions"])})
            continue

        prompt = safe_format_prompt(step_config["prompt"], **format_args)
        response_str = call_llm(
            llm_client,
            messages=[{"role": "user", "content": prompt}],
            json_mode=step_config["json_mode"],
            tag=f"{ui_key}:{micro_key}",
            extra_ctx={"micro_key": micro_key, "ui_key": ui_key}
        )
        try:
            result = json.loads(response_str.strip()) if step_config["json_mode"] else response_str.strip()
        except json.JSONDecodeError:
            st.error(f"无法解析JSON，模型返回内容: {response_str}")
            write_log("ERROR", "ui_section:json_parse_error", "微观组件JSON解析失败", {"micro_key": micro_key, "raw_snippet": _truncate_text(response_str, LOG_MAX_CONTENT_CHARS)})
            return

        ensure_version_state(micro_key)
        st.session_state[f"{micro_key}_versions"].append(result)
        st.session_state[f"{micro_key}_active_index"] = len(st.session_state[f"{micro_key}_versions"]) - 1
        st.session_state.data_timestamps[micro_key] = time.time()
        write_log("INFO", "ui_section:micro_generated", "微观组件生成完成", {"micro_key": micro_key, "ui_key": ui_key})

    # --- 步骤 2: 组装章节初稿（增强兜底，并记录组装结果） ---
    brief = st.session_state.get('structured_brief', {}) or {}
    content = ""

    if ui_key == "title":
        raw_options = get_active_content("title_options") or []
        titles = dedup_and_clean_titles(normalize_title_options(raw_options))
        if "title_versions" not in st.session_state:
            st.session_state.title_versions = []
        else:
            st.session_state.title_versions = dedup_and_clean_titles(st.session_state.title_versions)

        if not titles:
            core = (brief.get('core_inventive_concept') or '').strip()
            solution = (brief.get('technical_solution_summary') or '').strip()
            fallback = None
            if core and solution:
                fallback = f"一种基于{core}的{solution}"
            elif solution:
                fallback = f"一种{solution}"
            elif core:
                fallback = f"一种基于{core}的技术方案"
            if fallback:
                titles = [fallback]
                write_log("WARN", "ui_section:title_fallback", "使用结构化摘要兜底生成标题", {"fallback": fallback})

        if titles:
            st.session_state.title_versions.extend(titles)
            st.session_state.title_active_index = len(st.session_state.title_versions) - 1
            st.session_state.data_timestamps[ui_key] = time.time()
            write_log("INFO", "ui_section:title_built", "标题候选生成并保存", {"added_count": len(titles), "total_versions": len(st.session_state.title_versions)})
        else:
            st.warning("未能提取有效的发明名称候选，请重试或手动编辑。")
            write_log("WARN", "ui_section:title_empty", "未能提取有效标题候选")
        write_log("INFO", "ui_section:done", "章节生成完成", {"ui_key": ui_key})
        return

    elif ui_key == "background":
        context = (get_active_content("background_context") or "").strip()
        problem = (get_active_content("background_problem") or "").strip()
        if not context and brief.get("background_technology"):
            context = str(brief.get("background_technology"))
            write_log("WARN", "ui_section:background_fallback_context", "背景技术使用结构化摘要兜底")
        if not problem and brief.get("problem_statement"):
            problem = str(brief.get("problem_statement"))
            write_log("WARN", "ui_section:background_fallback_problem", "现有技术问题使用结构化摘要兜底")
        content = f"## 2.1 对最接近发明的同类现有技术状况加以分析说明\n{context}\n\n## 2.2 实事求是地指出现有技术存在的问题，尽可能分析存在的原因。\n{problem}"

    elif ui_key == "invention":
        purpose = (get_active_content("invention_purpose") or "").strip()
        solution_detail = (get_active_content("invention_solution_detail") or "").strip()
        effects = (get_active_content("invention_effects") or "").strip()
        if not purpose and brief.get("problem_statement") and brief.get("core_inventive_concept"):
            purpose = f"为解决{brief.get('problem_statement')}，提出基于{brief.get('core_inventive_concept')}的技术方案。"
            write_log("WARN", "ui_section:invention_fallback_purpose", "发明目的使用结构化摘要兜底")
        if not solution_detail and brief.get("technical_solution_summary"):
            solution_detail = str(brief.get("technical_solution_summary"))
            write_log("WARN", "ui_section:invention_fallback_solution", "技术解决方案使用结构化摘要兜底")
        if not effects and brief.get("achieved_effects"):
            effects = str(brief.get("achieved_effects"))
            write_log("WARN", "ui_section:invention_fallback_effects", "技术效果使用结构化摘要兜底")
        content = f"## 3.1 发明目的\n{purpose}\n\n## 3.2 技术解决方案\n{solution_detail}\n\n## 3.3 技术效果\n{effects}"

    elif ui_key == "implementation":
        details = get_active_content("implementation_details") or []
        if isinstance(details, list) and not details:
            sol = (get_active_content("invention_solution_detail") or brief.get("technical_solution_summary") or "").strip()
            if sol:
                details = [
                    f"实施例1：根据上述技术解决方案，系统由关键模块构成并按设计流程运行。核心方案：{sol}",
                    "实施例2：在不同环境与约束条件下的参数设定与安全策略调整。",
                    "实施例3：与现有系统的接口与数据交互、以及性能评估与可靠性保障。",
                ]
                write_log("WARN", "ui_section:implementation_fallback", "实施例细节为空，使用兜底条目")
        content = "\n".join([f"{i+1}. {detail}" for i, detail in enumerate(details)])

    elif ui_key in ("technical_field", "tech_field"):
        content = _fallback_technical_field(brief)

    elif ui_key in ("claims", "claim"):
        content = _fallback_claims(brief)

    elif ui_key in ("abstract", "summary"):
        content = _fallback_abstract(brief)

    else:
        parts: List[str] = []
        for micro_key in workflow_keys:
            block = get_active_content(micro_key)
            text = _stringify_block(block)
            if text:
                heading = WORKFLOW_CONFIG.get(micro_key, {}).get("title") or WORKFLOW_CONFIG.get(micro_key, {}).get("label")
                parts.append(f"{'## ' + heading if heading else ''}\n{text}".strip())
        content = "\n\n".join([p for p in parts if p.strip()])
        if not content.strip():
            if ui_key in ("drawings_description", "figures_description", "figures_desc"):
                content = _fallback_drawings_desc() if st.session_state.get("skip_drawings", SKIP_DRAWINGS_DEFAULT) else ""
            elif ui_key in ("technical_field", "tech_field"):
                content = _fallback_technical_field(brief)
            elif ui_key in ("claims", "claim"):
                content = _fallback_claims(brief)
            elif ui_key in ("abstract", "summary"):
                content = _fallback_abstract(brief)

    if not content.strip():
        st.warning(f"无法为 {UI_SECTION_CONFIG[ui_key]['label']} 生成初稿，依赖项内容为空。")
        write_log("WARN", "ui_section:empty_content", "章节初稿内容为空", {"ui_key": ui_key})
        return

    ensure_version_state(ui_key)
    st.session_state[f"{ui_key}_versions"].append(content)
    st.session_state[f"{ui_key}_active_index"] = len(st.session_state[f"{ui_key}_versions"]) - 1
    st.session_state.data_timestamps[ui_key] = time.time()

    write_log("INFO", "ui_section:assembled", "章节初稿组装并保存", {
        "ui_key": ui_key,
        "content_len": len(content),
        "content_snippet": _truncate_text(content, LOG_MAX_CONTENT_CHARS),
        "versions_count": len(st.session_state[f"{ui_key}_versions"])
    })
    write_log("INFO", "ui_section:done", "章节生成完成", {"ui_key": ui_key})

# -------------- 全局重构与润色 --------------

def run_global_refinement(llm_client: LLMClient):
    """迭代所有章节，并根据全局上下文和原始生成要求进行重构与润色。"""
    write_log("INFO", "global_refinement:start", "开始全局重构与润色")
    st.session_state.globally_refined_draft = {}
    initial_draft_content = {key: get_active_content(key) for key in UI_SECTION_ORDER}

    prompt_map = {
        "background": [prompts.PROMPT_BACKGROUND_CONTEXT, prompts.PROMPT_BACKGROUND_PROBLEM],
        "invention": [prompts.PROMPT_INVENTION_PURPOSE, prompts.PROMPT_INVENTION_SOLUTION_DETAIL, prompts.PROMPT_INVENTION_EFFECTS],
        "implementation": [prompts.PROMPT_IMPLEMENTATION_POINT]
    }

    with st.status("正在执行全局重构与润色...", expanded=True) as status:
        for target_key in UI_SECTION_ORDER:
            if target_key in ('drawings', 'figures', 'drawings_description', 'figures_description', 'figures_desc'):
                st.session_state.globally_refined_draft[target_key] = initial_draft_content.get(target_key)
                write_log("INFO", "global_refinement:skip", "跳过章节（无需润色）", {"target_key": target_key})
                continue

            status.update(label=f"正在重构与润色: {UI_SECTION_CONFIG[target_key]['label']}...")
            write_log("INFO", "global_refinement:section_start", "开始润色章节", {"target_key": target_key})

            global_context_parts = []
            for key, content in initial_draft_content.items():
                if key == target_key:
                    continue
                label = UI_SECTION_CONFIG[key]['label']
                processed_content = ""
                if key == 'title':
                    processed_content = content or ""
                elif key in ('drawings', 'figures') and isinstance(content, list):
                    processed_content = "附图列表:\n" + "\n".join([f"- {d.get('title')}: {d.get('description')}" for d in content])
                elif isinstance(content, str):
                    processed_content = content
                if processed_content:
                    global_context_parts.append(f"--- {label} ---\n{processed_content}")
            global_context = "\n".join(global_context_parts)
            target_content = initial_draft_content.get(target_key, "") or ""

            original_prompts = prompt_map.get(target_key, [])
            original_generation_prompt = "\n---\n".join(original_prompts)
            if not original_generation_prompt:
                st.warning(f"未找到 {UI_SECTION_CONFIG[target_key]['label']} 的原始生成指令，将仅基于全局上下文进行润色。")
                write_log("WARN", "global_refinement:no_original_prompt", "缺少原始生成指令", {"target_key": target_key})

            refine_prompt = safe_format_prompt(
                prompts.PROMPT_GLOBAL_RESTRUCTURE_AND_POLISH,
                global_context=global_context,
                target_section_name=UI_SECTION_CONFIG[target_key]['label'],
                target_section_content=target_content,
                original_generation_prompt=original_generation_prompt or ""
            )
            refined_content = call_llm(
                llm_client,
                messages=[{"role": "user", "content": refine_prompt}],
                json_mode=False,
                tag=f"refine:{target_key}",
                extra_ctx={"target_key": target_key}
            )
            st.session_state.globally_refined_draft[target_key] = (refined_content or "").strip()
            write_log("INFO", "global_refinement:refined", "章节润色完成", {"target_key": target_key, "refined_len": len(refined_content)})

        status.update(label="✅ 全局重构与润色完成！", state="complete")
    st.session_state.refined_version_available = True
    write_log("INFO", "global_refinement:done", "全局重构与润色完成")
