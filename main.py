import streamlit as st
import json
import time
from typing import Any

# --- 从模块导入 ---
import prompts
from config import UI_SECTION_ORDER, UI_SECTION_CONFIG
from llm_client import LLMClient
from state_manager import (
    initialize_session_state,
    get_active_content,
    is_stale,
)
from ui_components import (
    render_sidebar,
    render_mermaid_component,
    clean_mermaid_code,
)
from workflows import (
    generate_ui_section,
    generate_all_drawings,
    run_global_refinement,
    call_llm,  # 统一模型调用与日志记录
)
from auth import AuthManager, check_authentication

# --- 安全模板格式化辅助函数 ---
def safe_format_prompt(template: str, **kwargs) -> str:
    escaped = template.replace("{", "{{").replace("}", "}}")
    for k in kwargs.keys():
        escaped = escaped.replace(f"{{{{{k}}}}}", f"{{{k}}}")
    return escaped.format(**kwargs)

# --- 状态与通用工具 ---

def ensure_skip_drawings_state():
    # 与 workflows 中的 SKIP_DRAWINGS_DEFAULT 对齐，默认跳过附图
    if "skip_drawings" not in st.session_state:
        st.session_state.skip_drawings = True

def add_new_version(key: str, content: Any):
    """
    为指定key添加一个新版本，更新状态并触发UI刷新。
    兼容动态新增章节（如“附图说明”“附图标号表”“权利要求书”等），无需预初始化。
    """
    if f"{key}_versions" not in st.session_state:
        st.session_state[f"{key}_versions"] = []
    st.session_state[f"{key}_versions"].append(content)

    st.session_state[f"{key}_active_index"] = len(st.session_state[f"{key}_versions"]) - 1
    if "data_timestamps" not in st.session_state:
        st.session_state.data_timestamps = {}
    st.session_state.data_timestamps[key] = time.time()
    st.rerun()

# --- 阶段渲染函数 ---

def render_input_stage(llm_client: LLMClient):
    """渲染阶段一：输入核心技术构思"""
    st.header("Step 1️⃣: 输入核心技术构思")
    user_input = st.text_area(
        "在此处粘贴您的技术交底、项目介绍、或任何描述发明的文字：",
        value=st.session_state.user_input,
        height=250,
        key="user_input_area"
    )
    if st.button("🔬 分析并提炼核心要素", type="primary"):
        if user_input:
            st.session_state.user_input = user_input
            prompt = safe_format_prompt(prompts.PROMPT_ANALYZE, user_input=user_input)
            with st.spinner("正在调用分析代理，请稍候..."):
                try:
                    response_str = call_llm(
                        llm_client,
                        messages=[{"role": "user", "content": prompt}],
                        json_mode=True,
                        tag="analyze_brief",
                        extra_ctx={"stage": "input"}
                    )
                    st.session_state.structured_brief = json.loads(response_str.strip())
                    st.session_state.stage = "review_brief"
                    st.rerun()
                except (json.JSONDecodeError, KeyError) as e:
                    st.error(f"无法解析模型返回的核心要素，请检查模型输出或尝试调整输入。错误: {e}\n模型原始返回: \n{response_str}")
        else:
            st.warning("请输入您的技术构思。")

def render_review_brief_stage(llm_client: LLMClient):
    """渲染阶段二：审核并确认核心要素"""
    st.header("Step 2️⃣: 审核核心要素并选择模式")
    st.info("请检查并编辑AI提炼的发明核心信息。为保证后续章节的一致性，请以规范JSON编辑关键组件/步骤。")

    ensure_skip_drawings_state()
    st.checkbox("跳过附图生成（当前模型不支持文生图/图形生成）", value=st.session_state.skip_drawings, key="skip_drawings")

    brief = st.session_state.structured_brief
    def update_brief_timestamp():
        st.session_state.data_timestamps['structured_brief'] = time.time()

    brief['background_technology'] = st.text_area("背景技术", value=brief.get('background_technology', ''), on_change=update_brief_timestamp)
    brief['problem_statement'] = st.text_area("待解决的技术问题", value=brief.get('problem_statement', ''), on_change=update_brief_timestamp)
    brief['core_inventive_concept'] = st.text_area("核心创新点", value=brief.get('core_inventive_concept', ''), on_change=update_brief_timestamp)
    brief['technical_solution_summary'] = st.text_area("技术方案概述", value=brief.get('technical_solution_summary', ''), on_change=update_brief_timestamp)

    st.markdown("关键组件/步骤清单（严格JSON数组，每项包含 name 与 function）")
    init_components = brief.get('key_components_or_steps', [])
    if not (isinstance(init_components, list) and init_components and isinstance(init_components[0], dict)):
        init_components = [{"name": "", "function": ""}]
    components_json_text = st.text_area(
        "JSON编辑区",
        value=json.dumps(init_components, ensure_ascii=False, indent=2),
        height=200,
        key="key_components_json_edit"
    )
    col_json_save, col_json_help = st.columns([1, 1])
    with col_json_save:
        if st.button("💾 保存关键组件JSON"):
            try:
                parsed = json.loads(components_json_text)
                if isinstance(parsed, list) and all(isinstance(x, dict) and "name" in x and "function" in x for x in parsed):
                    brief['key_components_or_steps'] = parsed
                    update_brief_timestamp()
                    st.success("关键组件/步骤JSON已保存。")
                else:
                    st.error("JSON格式不符合要求：必须是数组，且每项包含 name 与 function。")
            except json.JSONDecodeError as e:
                st.error(f"JSON解析失败：{e}")
    with col_json_help:
        st.caption("提示：保持术语一致，有助于后续“附图标号表”和“权利要求书”生成。")

    brief['achieved_effects'] = st.text_area("有益效果（可量化表述，逐行）", value=brief.get('achieved_effects', ''), on_change=update_brief_timestamp)

    col1, col2, col3 = st.columns([2,2,1])
    if col1.button("🚀 一键生成初稿", type="primary"):
        with st.status("正在为您生成完整专利初稿...", expanded=True) as status:
            # 先生成 UI_SECTION_ORDER 中的所有键
            for key in UI_SECTION_ORDER:
                status.update(label=f"正在生成: {UI_SECTION_CONFIG[key]['label']}...")
                generate_ui_section(llm_client, key)
            # 补齐组合章节键，避免预览为空（仅在配置存在的情况下）
            COMPOSITE_SECTION_KEYS = ["title", "technical_field", "background", "invention", 
                                "figure_description", "implementation", "claims", "abstract", "drawings"]

            for k in COMPOSITE_SECTION_KEYS:
                if (k in UI_SECTION_CONFIG) and (not get_active_content(k)):
                    label = UI_SECTION_CONFIG.get(k, {}).get('label', k)
                    status.update(label=f"正在生成: {label}...")
                    generate_ui_section(llm_client, k)
            status.update(label="✅ 所有章节生成完毕！", state="complete")
        st.session_state.stage = "writing"
        st.rerun()

    if col2.button("✍️ 进入分步精修模式"):
        st.session_state.stage = "writing"
        st.rerun()

    if col3.button("返回重新输入"):
        st.session_state.stage = "input"
        st.rerun()

def render_writing_stage(llm_client: LLMClient):
    """渲染阶段三：分步生成与撰写（按专利结构标准）"""
    st.header("Step 3️⃣: 逐章生成与编辑专利草稿")

    if st.button("⬅️ 返回修改核心要素"):
        st.session_state.stage = "review_brief"
        st.rerun()

    st.markdown("---")
    just_generated_key = st.session_state.pop('just_generated_key', None)

    for key in UI_SECTION_ORDER:
        config = UI_SECTION_CONFIG[key]
        label = config["label"]
        versions = st.session_state.get(f"{key}_versions", [])
        is_section_stale = is_stale(key)

        expander_label = f"**{label}**"
        if is_section_stale:
            expander_label += " ⚠️ (依赖项已更新，建议重新生成)"
        elif not versions:
            expander_label += " (待生成)"

        is_expanded = (not versions) or is_section_stale or (key == just_generated_key)
        with st.expander(expander_label, expanded=is_expanded):
            # 专用渲染器：附图与权利要求书
            if key == 'drawings':
                render_drawings_section(llm_client)
                continue
            if key == 'claims':
                render_claims_section(llm_client, key, versions)
                continue

            render_standard_section(llm_client, key, versions)

def render_drawings_section(llm_client: LLMClient):
    """渲染'附图'专属UI和逻辑，并支持生成附图说明与标号表"""
    ensure_skip_drawings_state()

    if st.session_state.skip_drawings:
        st.info("当前已配置为跳过附图生成（可在“Step 2️⃣ 审核核心要素”中关闭该选项）。")
        return

    if not get_active_content("invention_solution_detail"):
        st.info("请先生成“技术解决方案”章节。")
        return

    invention_solution_detail = get_active_content("invention_solution_detail")

    # 全量生成附图
    if st.button("💡 (重新)构思并生成所有附图", key="regen_all_drawings"):
        with st.spinner("正在为您重新生成全套附图..."):
            generate_all_drawings(llm_client, invention_solution_detail)
            st.rerun()

    drawings = get_active_content("drawings")
    if drawings:
        st.caption("为保证独立性，可对单个附图重新生成，或在下方编辑代码。")

        # 生成“附图说明”与“附图标号表”
        col_fd, col_fl = st.columns([1, 1])
        with col_fd:
            if st.button("🖼️ 生成附图说明"):
                mermaid_ideas_json = json.dumps([{"title": d.get("title", ""), "description": d.get("description", "")} for d in drawings], ensure_ascii=False)
                fd_prompt = safe_format_prompt(prompts.PROMPT_FIGURE_DESCRIPTION, mermaid_ideas=mermaid_ideas_json)
                with st.spinner("正在生成附图说明..."):
                    fd_text = call_llm(
                        llm_client,
                        messages=[{"role": "user", "content": fd_prompt}],
                        json_mode=False,
                        tag="figure_description",
                        extra_ctx={"section": "drawings"}
                    )
                    add_new_version('figure_description', fd_text)
        with col_fl:
            if st.button("🏷️ 生成附图标号表"):
                key_components = st.session_state.structured_brief.get('key_components_or_steps', [])
                kc_json = json.dumps(key_components, ensure_ascii=False)
                fl_prompt = safe_format_prompt(prompts.PROMPT_FIGURE_LABELS, key_components_or_steps=kc_json)
                with st.spinner("正在生成附图标号表..."):
                    fl_json_str = call_llm(
                        llm_client,
                        messages=[{"role": "user", "content": fl_prompt}],
                        json_mode=True,
                        tag="figure_labels",
                        extra_ctx={"section": "drawings"}
                    )
                    try:
                        json.loads(fl_json_str)
                        add_new_version('figure_labels', fl_json_str)
                        st.success("附图标号表已生成。")
                    except json.JSONDecodeError:
                        st.error("生成的附图标号表JSON解析失败，请重试。")

        for i, drawing in enumerate(drawings):
            with st.container(border=True):
                col1, col2 = st.columns([3, 1])
                col1.markdown(f"**附图 {i+1}: {drawing.get('title', '无标题')}**")
                if col2.button(f"🔄 重新生成此图", key=f"regen_drawing_{i}"):
                    with st.spinner(f"正在重新生成附图: {drawing.get('title', '无标题')}..."):
                        code_prompt = safe_format_prompt(
                            prompts.PROMPT_MERMAID_CODE,
                            title=drawing.get('title', ''),
                            description=drawing.get('description', ''),
                            invention_solution_detail=invention_solution_detail
                        )
                        new_code = call_llm(
                            llm_client,
                            messages=[{"role": "user", "content": code_prompt}],
                            json_mode=False,
                            tag=f"drawing_{i+1}",
                            extra_ctx={"section": "drawings"}
                        )
                        active_drawings = json.loads(json.dumps(get_active_content("drawings")))
                        active_drawings[i]["code"] = clean_mermaid_code(new_code)
                        add_new_version('drawings', active_drawings)

                st.markdown(f"**构思说明:** *{drawing.get('description', '无')}*")
                render_mermaid_component(f"mermaid_{i}", drawing)

                edited_code = st.text_area("编辑Mermaid代码:", value=drawing["code"], key=f"edit_code_{i}", height=150)
                if edited_code != drawing["code"]:
                    active_drawings = json.loads(json.dumps(get_active_content("drawings")))
                    active_drawings[i]["code"] = edited_code
                    add_new_version('drawings', active_drawings)

def render_claims_section(llm_client: LLMClient, key: str, versions: list):
    """渲染权利要求书章节，支持一致性与支持度校验"""
    config = UI_SECTION_CONFIG[key]
    label = config["label"]

    col1, col2, col3 = st.columns([2, 1, 1])
    with col1:
        deps_met = all(
            (st.session_state.get("structured_brief") if dep == "structured_brief" else get_active_content(dep))
            for dep in config["dependencies"]
        )
        if deps_met:
            if st.button(f"🔄 重新生成 {label}" if versions else f"✍️ 生成 {label}", key=f"btn_{key}"):
                with st.spinner(f"正在执行 {label} 的生成流程..."):
                    generate_ui_section(llm_client, key)
                    st.session_state.just_generated_key = key
                    st.rerun()
        else:
            st.info(f"请先生成前置章节: {', '.join(config['dependencies'])}")

    # 版本选择
    active_idx = st.session_state.get(f"{key}_active_index", 0)
    if len(versions) > 1:
        with col2:
            version_labels = [f"版本 {i+1}" for i in range(len(versions))]
            new_idx = st.selectbox(f"选择版本", version_labels, index=active_idx, key=f"select_{key}")
            active_idx = version_labels.index(new_idx)
            if active_idx != st.session_state.get(f"{key}_active_index", 0):
                st.session_state[f"{key}_active_index"] = active_idx
                st.rerun()

    # 一致性校验按钮
    with col3:
        if get_active_content(key):
            if st.button("🧪 权利要求一致性校验"):
                claims_text = get_active_content(key)
                global_context = assemble_global_context_for_claims_check()
                kc_json = json.dumps(st.session_state.structured_brief.get('key_components_or_steps', []), ensure_ascii=False)
                check_prompt = safe_format_prompt(
                    prompts.PROMPT_CLAIMS_CHECK,
                    claims_text=claims_text,
                    global_context=global_context,
                    key_components_or_steps=kc_json
                )
                with st.spinner("正在执行权利要求支持度校验..."):
                    check_str = call_llm(
                        llm_client,
                        messages=[{"role": "user", "content": check_prompt}],
                        json_mode=True,
                        tag="claims_check",
                        extra_ctx={"section": "claims"}
                    )
                    try:
                        check_report = json.loads(check_str)
                        st.session_state.claims_check_report = check_report
                        st.success("校验完成。")
                    except json.JSONDecodeError as e:
                        st.error(f"校验报告解析失败：{e}")

    # 编辑区
    if versions:
        active_content = get_active_content(key)

        with st.form(key=f'form_edit_{key}'):
            edited_content = st.text_area("编辑区（权利要求全文）", value=active_content, height=300)
            submitted = st.form_submit_button("💾 保存修改 (快捷键: Ctrl+Enter)")
            if submitted and edited_content != active_content:
                add_new_version(key, edited_content)

    # 显示校验报告
    if "claims_check_report" in st.session_state:
        st.markdown("**权利要求支持度校验报告**")
        try:
            report = st.session_state.claims_check_report
            for item in report:
                supported_str = "✅ 支持" if item.get("supported") else "❌ 不完全支持"
                st.write(f"权利要求 {item.get('claim_no')}: {supported_str}")
                if item.get("unsupported_elements"):
                    st.write("缺乏依据的要素/限定：")
                    st.write(", ".join(item.get("unsupported_elements")))
                if item.get("recommended_actions"):
                    st.write("修订建议：")
                    for act in item.get("recommended_actions"):
                        st.write(f"- {act}")
        except Exception:
            st.write("校验报告显示失败，请重试。")

def render_standard_section(llm_client: LLMClient, key: str, versions: list):
    """渲染标准章节的UI和逻辑（非附图/非权利要求）"""
    config = UI_SECTION_CONFIG[key]
    label = config["label"]

    col1, col2 = st.columns([3, 1])
    with col1:
        deps_met = all(
            (st.session_state.get("structured_brief") if dep == "structured_brief" else get_active_content(dep))
            for dep in config["dependencies"]
        )
        if deps_met:
            if st.button(f"🔄 重新生成 {label}" if versions else f"✍️ 生成 {label}", key=f"btn_{key}"):
                with st.spinner(f"正在执行 {label} 的生成流程..."):
                    generate_ui_section(llm_client, key)
                    st.session_state.just_generated_key = key
                    st.rerun()
        else:
            st.info(f"请先生成前置章节: {', '.join(config['dependencies'])}")

    active_idx = st.session_state.get(f"{key}_active_index", 0)
    if len(versions) > 1:
        with col2:
            version_labels = [f"版本 {i+1}" for i in range(len(versions))]
            new_idx = st.selectbox(f"选择版本", version_labels, index=active_idx, key=f"select_{key}")
            active_idx = version_labels.index(new_idx)
            if active_idx != st.session_state.get(f"{key}_active_index", 0):
                st.session_state[f"{key}_active_index"] = active_idx
                st.rerun()

    if versions:
        active_content = get_active_content(key)

        with st.form(key=f'form_edit_{key}'):
            if key == 'title':
                edited_content = st.text_input("编辑区", value=active_content)
            else:
                edited_content = st.text_area("编辑区", value=active_content, height=300)

            submitted = st.form_submit_button("💾 保存修改 (快捷键: Ctrl+Enter)")

            if submitted and edited_content != active_content:
                add_new_version(key, edited_content)

def render_preview_stage(llm_client: LLMClient):
    """渲染阶段四：预览、精炼与下载（符合专利结构标准）"""
    st.header("Step 4️⃣: 预览、精炼与下载")
    st.markdown("---")

    if st.button("✨ 全局重构与润色", type="primary", help="调用顶级专利总编AI，对所有章节进行深度重构、润色和细节补充，确保全文逻辑、深度和专业性达到最佳状态。"):
        run_global_refinement(llm_client)
        st.rerun()

    tabs = ["✍️ 初稿"]
    if st.session_state.get("refined_version_available"):
        tabs.append("✨ 全局重构润色版")

    selected_tab = st.radio("选择预览版本", tabs, horizontal=True)

    if selected_tab == "✍️ 初稿":
        draft_data = {key: get_active_content(key) for key in UI_SECTION_ORDER}
        draft_data["figure_description"] = get_active_content("figure_description")
        draft_data["figure_labels"] = get_active_content("figure_labels")
        st.subheader("初稿预览")
    else:  # 全局精炼版
        draft_data = st.session_state.globally_refined_draft
        st.subheader("全局重构润色版预览")

    # 章节正文直接取整段内容，若缺失则用微观子键兜底拼接
    title = draft_data.get('title', '无标题')
    tech_field = draft_data.get('technical_field') or draft_data.get('tech_field') or ''

    background_full = draft_data.get('background') or (
        f"## 2.1 对最接近发明的同类现有技术状况加以分析说明\n{draft_data.get('background_context','')}\n\n"
        f"## 2.2 实事求是地指出现有技术存在的问题，尽可能分析存在的原因。\n{draft_data.get('background_problem','')}"
    )

    invention_full = draft_data.get('invention') or (
        f"## 3.1 发明目的\n{draft_data.get('invention_purpose','')}\n\n"
        f"## 3.2 技术解决方案\n{draft_data.get('invention_solution_detail','')}\n\n"
        f"## 3.3 技术效果\n{draft_data.get('invention_effects','')}"
    )

    implementation = draft_data.get('implementation', '')
    claims_text = draft_data.get('claims', '')
    abstract_text = draft_data.get('abstract', '')

    # 附图说明与标号表（若跳过附图，则用占位）
    if st.session_state.get("skip_drawings", True):
        figure_description_text = "（本申请无附图）"
        figure_labels_text = ""
    else:
        figure_description_text = draft_data.get('figure_description', '') or '（附图说明待补充）'
        figure_labels = draft_data.get("figure_labels")
        figure_labels_text = ""
        if figure_labels:
            try:
                labels = json.loads(figure_labels) if isinstance(figure_labels, str) else figure_labels
                figure_labels_text = "附图标号表：\n" + "\n".join([f"{item.get('id','')}: {item.get('name','')} - {item.get('description','')}" for item in labels])
            except Exception:
                figure_labels_text = "附图标号表解析失败。"

    # 附图（Mermaid）
    drawings_text = ""
    drawings = draft_data.get("drawings")
    if drawings and isinstance(drawings, list) and not st.session_state.get("skip_drawings", True):
        for i, drawing in enumerate(drawings):
            drawings_text += f"## 附图{i+1}：{drawing.get('title', '')}\n"
            drawings_text += f"```mermaid\n{drawing.get('code', '')}\n```\n\n"

    full_text = (
        f"# 一、发明名称\n{title}\n\n"
        f"# 二、技术领域\n{tech_field}\n\n"
        f"# 三、背景技术\n{background_full}\n\n"
        f"# 四、发明内容\n{invention_full}\n\n"
        f"# 五、附图说明\n{figure_description_text}\n\n"
        f"{figure_labels_text if figure_labels_text else ''}\n\n"
        f"# 六、具体实施方式\n{implementation}\n\n"
        f"# 七、权利要求书\n{claims_text}\n\n"
        f"# 八、摘要\n{abstract_text}\n\n"
        f"# 九、附图\n{drawings_text if drawings_text else '（本申请无附图）'}\n"
    )

    st.subheader("完整草稿预览")
    st.markdown(full_text)
    st.download_button("📄 下载当前预览版本 (.md)", full_text, file_name=f"{title}_patent_draft.md")

# --- 权利要求校验上下文组装 ---

def assemble_global_context_for_claims_check() -> str:
    """
    组装用于权利要求一致性校验的说明书全文上下文。
    使用已组装的整段章节，确保上下文完整；若缺失则兜底。
    """
    tech_field = get_active_content("technical_field") or get_active_content("tech_field") or ""
    background = get_active_content("background") or (
        f"## 2.1 对最接近发明的同类现有技术状况加以分析说明\n{get_active_content('background_context') or ''}\n\n"
        f"## 2.2 实事求是地指出现有技术存在的问题，尽可能分析存在的原因。\n{get_active_content('background_problem') or ''}"
    )
    invention = get_active_content("invention") or (
        f"## 3.1 发明目的\n{get_active_content('invention_purpose') or ''}\n\n"
        f"## 3.2 技术解决方案\n{get_active_content('invention_solution_detail') or ''}\n\n"
        f"## 3.3 技术效果\n{get_active_content('invention_effects') or ''}"
    )
    implementation = get_active_content("implementation") or ""

    ctx = (
        f"技术领域：{tech_field}\n"
        f"背景技术：{background}\n"
        f"发明内容：{invention}\n"
        f"具体实施方式：{implementation}\n"
    )
    return ctx

# --- 主应用逻辑 ---

def main():
    st.set_page_config(page_title="智能专利撰写助手", layout="wide", page_icon="📝")

    # 初始化认证管理器
    auth_manager = AuthManager()

    # 检查认证状态
    if not check_authentication(auth_manager):
        return

    # 认证通过后显示主界面
    st.title("📝 智能专利申请书撰写助手")
    st.caption("新功能：支持权利要求一致性校验、附图说明与标号表生成。")

    initialize_session_state()
    ensure_skip_drawings_state()
    config = st.session_state.config
    render_sidebar(config)

    active_provider = st.session_state.config["provider"]
    if not st.session_state.config.get(active_provider, {}).get("api_key"):
        st.warning("请在左侧边栏配置并保存您的 API Key。")
        st.stop()

    if 'llm_client' not in st.session_state or st.session_state.llm_client.full_config != st.session_state.config:
        st.session_state.llm_client = LLMClient(st.session_state.config)
    llm_client = st.session_state.llm_client

    # 使用分派字典来调用对应阶段的渲染函数
    stage_renderers = {
        "input": render_input_stage,
        "review_brief": render_review_brief_stage,
        "writing": render_writing_stage,
    }

    renderer = stage_renderers.get(st.session_state.stage)
    if renderer:
        renderer(llm_client)

    # 预览阶段是写作阶段的一部分，在写作阶段的末尾渲染
    if st.session_state.stage == "writing":
        render_preview_stage(llm_client)


if __name__ == "__main__":
    main()