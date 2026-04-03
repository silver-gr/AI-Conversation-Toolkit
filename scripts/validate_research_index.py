#!/usr/bin/env python3
"""
Validate output/RESEARCH_INDEX.md against the exported conversation markdown and
generate:

1. output/RESEARCH_INDEX_VALIDATION.md
2. output/RESEARCH_RERUN_GRADES.md

This script uses a conservative, high-confidence curation pass:
- remove clearly invalid indexed entries
- add clearly missing research-grade threads
- score validated threads for rerun-worthiness in 2026
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = BASE_DIR / "output"
INDEX_PATH = OUTPUT_DIR / "RESEARCH_INDEX.md"
AUDIT_STAMP = "20260309_v4"
VALIDATION_PATH = OUTPUT_DIR / f"RESEARCH_INDEX_VALIDATION_AUDIT_{AUDIT_STAMP}.md"
RERUN_PATH = OUTPUT_DIR / f"RESEARCH_RERUN_GRADES_AUDIT_{AUDIT_STAMP}.md"
ERROR_REPORT_PATH = OUTPUT_DIR / f"RESEARCH_INDEX_ERROR_REPORT_AUDIT_{AUDIT_STAMP}.md"
FIXED_REPORT_PATH = OUTPUT_DIR / f"RESEARCH_INDEX_FIXED_REPORT_{AUDIT_STAMP}.md"
STRICT_FIXED_REPORT_PATH = OUTPUT_DIR / f"RESEARCH_INDEX_FIXED_REPORT_STRICT_EXECUTED_{AUDIT_STAMP}.md"


INVALID_INDEXED: dict[str, str] = {
    # ChatGPT
    "chatgpt-full/conversations/20260226_wolf-buddha-ζωή.md": "Translation of a prewritten report, not a research run.",
    "chatgpt-full/conversations/20260220_chatgpt-deep-research-limits.md": "Meta discussion about Deep Research quotas/UI, not a deep-research thread.",
    "chatgpt-full/conversations/20260121_μετάφραση-και-βελτιστοποίηση-tts.md": "Translation/formatting task, not original research.",
    "chatgpt-full/conversations/20251206_what-i-learned-about-you.md": "Personal summary chat, not external research.",
    "chatgpt-full/conversations/20251120_rate-limits-for-pro.md": "Rate-limit lookup/meta tool question, not a research report thread.",
    "chatgpt-full/conversations/20251030_translate-to-greek.md": "Pure translation task.",
    "chatgpt-full/conversations/20251024_research-plan-validation.md": "Research-plan drafting/validation, not the execution of the research.",
    "chatgpt-full/conversations/20251024_research-task-clarification.md": "Prompt/task clarification rather than a completed research run.",
    "chatgpt-full/conversations/20251024_research-plan-clarification.md": "Prompt/task clarification rather than a completed research run.",
    "chatgpt-full/conversations/20251024_research-protocol-clarification.md": "Protocol drafting rather than a completed research run.",
    "chatgpt-full/conversations/20251024_deep-research-ideas.md": "Idea generation for future research, not a research thread itself.",
    "chatgpt-full/conversations/20251015_mastering-deep-research.md": "Guide/prompt-writing about Deep Research, not a research run.",
    "chatgpt-full/conversations/20250812_research-topics-for-x3lixi.md": "Topic ideation for future research, not a completed research thread.",
    "chatgpt-full/conversations/20250801_gemini-deep-research-overview.md": "Overview of Gemini Deep Research as a tool, not a research run.",
    "chatgpt-full/conversations/20250801_gemini-deep-research-tips.md": "Prompt/tips chat about using the tool, not a research thread.",
    "chatgpt-full/conversations/20250729_obsidian-vault-cleanup-guide.md": "Prompt-generation/analysis request based on a local vault, not external research.",
    "chatgpt-full/conversations/20250718_research-prompts-for-self-help.md": "Prompt-generation, not research execution.",
    "chatgpt-full/conversations/20250718_research-prompts-for-marketing.md": "Prompt-generation, not research execution.",
    "chatgpt-full/conversations/20250306_deep-research-domains.md": "Drafting domains/prompts for future Deep Research, not a research thread.",
    # Claude
    "claude-full/conversations/20260116_separating-scientific-and-anecdotal-know.md": "Conceptual discussion, not a research-tool/web-research run.",
    "claude-full/conversations/20251225_x3lixios-obsidian-integration-and-workfl.md": "Internal workflow planning, not external research.",
    "claude-full/conversations/20251214_using-research-allowance-effectively.md": "Meta discussion about how to spend research allowance.",
    "claude-full/conversations/20251204_managing-medication-breaks-and-ambitious.md": "Personal planning/journaling, not research-tool usage.",
    "claude-full/conversations/20251120_project-chat-index-with-descriptions.md": "Internal chat indexing, not external research.",
    "claude-full/conversations/20251120_chat-analysis-and-prioritization-index.md": "Internal chat indexing, not external research.",
    "claude-full/conversations/20251119_using-expiring-claude-credits-for-projec.md": "Meta planning about how to spend Claude credits.",
    "claude-full/conversations/20251112_r-gpt-5-pro-and-gemini-25-pro-deep-think.md": "Use-case brainstorming, not an executed research report.",
    "claude-full/conversations/20251112_creating-successful-online-courses-with-.md": "Course-creation ideation, not a research run.",
    "claude-full/conversations/20251124_multi-platform-content-strategy-prompt-f.md": "Prompt creation for another model, not the research itself.",
    "claude-full/conversations/20251017_creating-a-greek-meditation-course-with-.md": "Course/planning chat, not a research-tool/web-research thread.",
    "claude-full/conversations/20251014_gemini-deep-research-guide.md": "Guide about Gemini Deep Research, not a research thread.",
    "claude-full/conversations/20251014_claude-research-tool-guide.md": "Guide about Claude's research tool, not a research thread.",
    "claude-full/conversations/20251015_claude-research-tool-guide.md": "Guide about Claude's research tool, not a research thread.",
    "claude-full/conversations/20251010_cross-platform-conversation-extraction-p.md": "Prompt/spec generation rather than a research run.",
    "claude-full/conversations/20250828_bdsm-dominance-training-curriculum-promp.md": "Prompt-writing/template task, not research execution.",
    "claude-full/conversations/20250722_dopamine-research-strategy-with-ai-tools.md": "Strategy for doing research with AI tools, not a completed research thread.",
    # Gemini
    "gemini/conversations/20260130_create-a-prompt-to-think-and-research-ho.md": "Prompt engineering chat, not Gemini Deep Research execution.",
    "gemini/conversations/20260105_advanced-research-prompt-nac-induced-gas.md": "Only the research prompt itself; no completed Gemini research run.",
    "gemini/conversations/20251206_create-prompt-gpt-search-check-user.md": "Prompt construction chat; attempted research errors out.",
    "gemini/conversations/20250912_translate-the-deep-research-report-to-gr.md": "Translation of an existing report, not original research.",
    "gemini/conversations/20250802_create-meta-prompt-instruct.md": "Meta-prompt/template conversation.",
    "gemini/conversations/20250912_bdsm-power-exchange-infographic.md": "Canvas artifact from a mixed flow, not a clean Deep Research thread.",
}


MISSING_RESEARCH: dict[str, str] = {
    # ChatGPT
    "chatgpt-full/conversations/20260214_longevity-biohacks-2026.md": "High-signal evidence-ranked longevity report that matches research-grade indexed entries.",
    "chatgpt-full/conversations/20251204_deepstash-app-reconstruction-plan.md": "Explicit DeepStash reconstruction research report with sourced product teardown.",
    "chatgpt-full/conversations/20251108_micro-learning-app-comparison.md": "Substantive market comparison report for the micro-learning app space.",
    "chatgpt-full/conversations/20251024_systematic-review-milk-thistle.md": "Research-grade systematic review request and sourced synthesis.",
    "chatgpt-full/conversations/20251024_milk-thistle-analysis.md": "Research-grade evidence synthesis currently absent from the index.",
    "chatgpt-full/conversations/20251022_ai-browser-comparison.md": "Comprehensive comparative research report on AI browsers.",
    "chatgpt-full/conversations/20251019_bromantane-research-analysis.md": "Clinical-grade monograph/evidence synthesis missing from the index.",
    "chatgpt-full/conversations/20250718_hi-tech-psytrance-mixing-guide.md": "Explicit multi-question research report with citations and practical synthesis.",
    "chatgpt-full/conversations/20250707_cannabis-use-impact-analysis.md": "Research analysis of community evidence/discourse that fits the research index better than multiple included entries.",
    # Claude
    "claude-full/conversations/20260214_evidence-based-longevity-biohacks-ranked.md": "Clear web-research run that compiles an evidence-ranked biohacks report.",
    "claude-full/conversations/20260117_evaluating-long-context-metrics-in-front.md": "Clear comparative research run on frontier-model long-context metrics.",
    "claude-full/conversations/20260116_current-llm-market-share-breakdown.md": "Clear current-state market research report missing from the index.",
    "claude-full/conversations/20251210_frontier-llm-comparison-gemini-3-claude-.md": "Current benchmark and community-sentiment comparison that fits the indexed Claude research pattern.",
    "claude-full/conversations/20251130_claude-flow-swe-bench-benchmark-verifica.md": "Explicit benchmark-verification research run missing from the index.",
    # Gemini
    "gemini/conversations/20260302_analyzing-recent-trends-everyday-impact.md": "Substantial Canvas research report; the current index points to a stale broken filename instead.",
    "gemini/conversations/20260213_deepstash-rebuild-reference.md": "Full Canvas DeepStash rebuild analysis that matches indexed DeepStash research artifacts.",
    "gemini/conversations/20251108_messaging-platform-data-capture-research.md": "Explicit feasibility/risk research report with executive summary and platform breakdown.",
    "gemini/conversations/20251030_interactive-competitive-analysis.md": "Research-grade competitive-analysis report absent from the index.",
}


SOURCE_LABELS = {
    "chatgpt-full": "ChatGPT",
    "claude-full": "Claude",
    "gemini": "Gemini",
}


STRATEGIC_KEYWORDS = {
    "market",
    "competitive",
    "pricing",
    "customer",
    "app",
    "product",
    "landscape",
    "analysis",
    "compare",
    "comparison",
    "benchmark",
    "workflow",
    "architecture",
    "system",
    "rag",
    "obsidian",
    "feature",
    "rebuild",
    "teardown",
    "research report",
    "feasibility",
    "strategy",
}

EVIDENCE_KEYWORDS = {
    "evidence",
    "systematic review",
    "meta-analysis",
    "clinical",
    "scientific",
    "effect size",
    "pharmacology",
    "longevity",
    "interventions",
    "biohacks",
}

META_OR_STALE_KEYWORDS = {
    "rate limits",
    "quota",
    "how to use",
    "guide",
    "overview",
    "tips",
    "prompt",
    "template",
    "translation",
    "translate",
    "monthly limits",
    "tool guide",
}

PERSONAL_OR_EPHEMERAL_KEYWORDS = {
    "kink",
    "facefucked",
    "penis",
    "relationship",
    "breakup",
    "social anxiety",
    "dating",
}


@dataclass(frozen=True)
class Conversation:
    rel_path: str
    date: str
    source: str
    title: str
    first_query: str


def parse_frontmatter_value(content: str, key: str) -> str:
    match = re.search(rf"^{re.escape(key)}:\s*\"?(.*?)\"?$", content, re.MULTILINE)
    return match.group(1).strip() if match else ""


def extract_first_user(content: str) -> str:
    match = re.search(r"### USER.*?\n\n(.+?)(?=\n\n---|\n\n###)", content, re.DOTALL)
    if not match:
        return ""
    text = re.sub(r"\s+", " ", match.group(1)).strip()
    return text


def extract_assistant_messages(content: str) -> list[str]:
    return re.findall(
        r"### ASSISTANT.*?\n\n(.+?)(?=\n\n---|\n\n###|\Z)",
        content,
        re.DOTALL,
    )


def load_conversation(rel_path: str) -> Conversation:
    file_path = OUTPUT_DIR / rel_path
    content = file_path.read_text(encoding="utf-8", errors="ignore")
    title = parse_frontmatter_value(content, "title")
    date = parse_frontmatter_value(content, "date")[:10]
    source_key = rel_path.split("/")[0]
    return Conversation(
        rel_path=rel_path,
        date=date,
        source=SOURCE_LABELS[source_key],
        title=title,
        first_query=extract_first_user(content),
    )


def normalize_for_score(*parts: str) -> str:
    return " ".join(parts).lower()


def score_conversation(conv: Conversation) -> int:
    text = normalize_for_score(conv.title, conv.first_query)
    score = 5

    if any(keyword in text for keyword in STRATEGIC_KEYWORDS):
        score += 2
    if any(keyword in text for keyword in EVIDENCE_KEYWORDS):
        score += 2
    if "greece" in text or "greek" in text:
        score += 1
    if "2026" in text or "current" in text or "latest" in text:
        score -= 1
    if any(keyword in text for keyword in META_OR_STALE_KEYWORDS):
        score -= 2
    if any(keyword in text for keyword in PERSONAL_OR_EPHEMERAL_KEYWORDS):
        score -= 2

    high_value_titles = (
        "deepstash",
        "micro-learning app comparison",
        "ai self-help app research",
        "greek productivity market research",
        "systematic review",
        "bromantane",
        "competitive analysis",
        "messaging platform data capture research",
        "app feature analysis",
        "product analyst",
        "ux researcher",
        "feature analysis",
        "teardown",
        "rebuild",
        "blueprint",
        "market research",
        "fact-check",
        "benchmark",
        "long context",
        "current llm market share",
    )
    if any(marker in text for marker in high_value_titles):
        score += 2

    if "deep research tools overview" in text or "use-cases" in text:
        score -= 2
    if "overview" in text and "deep research" in text:
        score -= 1

    return max(1, min(10, score))


def clean_table_text(text: str, limit: int = 180) -> str:
    text = text.replace("|", "\\|").replace("\n", " ").strip()
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        return text[: limit - 3].rstrip() + "..."
    return text


def collect_index_links() -> set[str]:
    content = INDEX_PATH.read_text(encoding="utf-8")
    pattern = r"\((chatgpt-full/conversations/[^)]+|claude-full/conversations/[^)]+|gemini/conversations/[^)]+)\)"
    return set(re.findall(pattern, content))


def build_corrected_set(indexed_links: set[str]) -> set[str]:
    corrected = set(indexed_links)
    corrected -= set(INVALID_INDEXED)
    corrected |= set(MISSING_RESEARCH)
    corrected.discard("gemini/conversations/20260302_start-research.md")
    return corrected


def is_chatgpt_executed_deep_research(content: str) -> bool:
    model = parse_frontmatter_value(content, "model")
    user = extract_first_user(content).lower()
    assistants = extract_assistant_messages(content)
    last_assistant = assistants[-1] if assistants else ""
    has_research_json = '"task_violates_safety_guidelines": false' in content
    big_final = len(last_assistant) >= 1000 or last_assistant.count("## ") >= 3

    positive_markers = (
        "research",
        "deep research",
        "compare",
        "comparison",
        "analysis",
        "review",
        "investigation",
        "evidence",
        "benefits",
        "effects",
        "overview",
        "best",
        " vs ",
        " versus ",
        "organize",
        "alternatives",
        "teardown",
        "benchmark",
        "market",
    )
    negative_markers = (
        "translate",
        "translation",
        "prompt",
        "template",
        "improve my life in these areas",
        "what did you learn about me",
        "generate 5 chatgpt",
        "research topics for",
        "tips and advice on how to best utilize",
    )

    if any(marker in user for marker in negative_markers):
        return False
    if model == "research":
        return True
    return has_research_json and big_final and any(marker in user for marker in positive_markers)


def is_claude_executed_deep_research(content: str) -> bool:
    lower = content.lower()
    user = extract_first_user(content).lower()
    placeholder_blocks = lower.count("this block is not supported on your current device yet")

    positive_markers = (
        "research online",
        "search the web",
        "check the web",
        "search online",
        "community",
        "sentiment",
        "market share",
        "benchmark",
        "find all the info you can across the web",
        "do some digging",
        "compare",
        "current",
    )
    negative_markers = (
        "other chats",
        "project knowledge",
        "memory",
        "weekly review",
        "goals from",
        "using research allowance",
        "credits that expire",
        "guide",
        "prompt for",
        "summarize all our chats",
    )

    if placeholder_blocks < 2 or any(marker in user for marker in negative_markers):
        return False
    return any(marker in user for marker in positive_markers) or (
        "research" in user and ("web" in user or "online" in user)
    )


def is_gemini_executed_deep_research(content: str) -> bool:
    lower = content.lower()
    user = extract_first_user(content).lower()

    negative_markers = (
        "create a prompt",
        "meta-prompt",
        "translate the deep research report",
        "use the colors and style",
        "build this demo",
        "create a journeys page",
        "recreate a skills screen",
        "storybook",
        "logo",
        "styles.css",
        "write a detailed description of this application",
    )
    if any(marker in user for marker in negative_markers):
        return False

    explicit_complete = "i've completed your research" in lower
    explicit_plan = "here's a research plan" in lower
    explicit_execute = (
        "executing the research now" in lower
        or "understood. executing the research now" in lower
    )
    has_canvas = "### gemini (canvas)" in lower

    return has_canvas and (explicit_complete or (explicit_plan and explicit_execute))


def build_strict_executed_set(corrected_links: set[str]) -> set[str]:
    strict: set[str] = set()
    for rel_path in corrected_links:
        content = (OUTPUT_DIR / rel_path).read_text(encoding="utf-8", errors="ignore")
        if rel_path.startswith("chatgpt-full/") and is_chatgpt_executed_deep_research(content):
            strict.add(rel_path)
        elif rel_path.startswith("claude-full/") and is_claude_executed_deep_research(content):
            strict.add(rel_path)
        elif rel_path.startswith("gemini/") and is_gemini_executed_deep_research(content):
            strict.add(rel_path)
    return strict


def build_validation_report(indexed_links: set[str], corrected_links: set[str]) -> str:
    broken_links = sorted(link for link in indexed_links if not (OUTPUT_DIR / link).exists())
    invalid_by_source: dict[str, list[tuple[str, str]]] = {label: [] for label in SOURCE_LABELS.values()}
    missing_by_source: dict[str, list[tuple[str, str]]] = {label: [] for label in SOURCE_LABELS.values()}

    for rel_path, reason in sorted(INVALID_INDEXED.items()):
        source = SOURCE_LABELS[rel_path.split("/")[0]]
        invalid_by_source[source].append((rel_path, reason))

    for rel_path, reason in sorted(MISSING_RESEARCH.items()):
        source = SOURCE_LABELS[rel_path.split("/")[0]]
        missing_by_source[source].append((rel_path, reason))

    lines: list[str] = []
    lines.append("# Research Index Validation")
    lines.append("")
    lines.append("## Method")
    lines.append("")
    lines.append("This is a conservative, high-confidence validation pass.")
    lines.append("")
    lines.append("- Indexed entries were kept only when they looked like actual research runs.")
    lines.append("- Meta chats about Deep Research, prompt-writing, translations, and plan-only chats were removed.")
    lines.append("- Only clear missing research threads were added back in.")
    lines.append("- Borderline cases were intentionally left out of the corrected corpus unless the evidence was strong.")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Indexed links in `[RESEARCH_INDEX.md](RESEARCH_INDEX.md)`: **{len(indexed_links)}**")
    lines.append(f"- Clearly invalid indexed entries removed: **{len(INVALID_INDEXED)}**")
    lines.append(f"- Clearly missing research threads added: **{len(MISSING_RESEARCH)}**")
    lines.append(f"- Broken/stale links found in the current index: **{len(broken_links)}**")
    lines.append(f"- Corrected validated research corpus: **{len(corrected_links)}**")
    lines.append("")

    if broken_links:
        lines.append("## Broken Or Stale Links")
        lines.append("")
        for rel_path in broken_links:
            lines.append(f"- `{rel_path}`")
        lines.append("")

    lines.append("## Removed From Index")
    lines.append("")
    for source in ("ChatGPT", "Claude", "Gemini"):
        lines.append(f"### {source}")
        lines.append("")
        for rel_path, reason in invalid_by_source[source]:
            lines.append(f"- `{Path(rel_path).name}`: {reason}")
        lines.append("")

    lines.append("## Added To Corrected Corpus")
    lines.append("")
    for source in ("ChatGPT", "Claude", "Gemini"):
        lines.append(f"### {source}")
        lines.append("")
        for rel_path, reason in missing_by_source[source]:
            lines.append(f"- `{Path(rel_path).name}`: {reason}")
        lines.append("")

    lines.append("## Output")
    lines.append("")
    lines.append(f"- Corrected, scored table: `[{RERUN_PATH.name}]({RERUN_PATH.name})`")
    lines.append(f"- Error-pattern report: `[{ERROR_REPORT_PATH.name}]({ERROR_REPORT_PATH.name})`")
    lines.append("")
    return "\n".join(lines)


def build_error_report(indexed_links: set[str]) -> str:
    broken_links = sorted(link for link in indexed_links if not (OUTPUT_DIR / link).exists())
    source_invalid_counts = {"ChatGPT": 0, "Claude": 0, "Gemini": 0}
    for rel_path in INVALID_INDEXED:
        source_invalid_counts[SOURCE_LABELS[rel_path.split("/")[0]]] += 1

    lines: list[str] = []
    lines.append("# Research Index Error Report")
    lines.append("")
    lines.append("## What Went Wrong")
    lines.append("")
    lines.append("This report documents the error patterns visible in the current `[RESEARCH_INDEX.md](RESEARCH_INDEX.md)` and the most likely mishandling behind them.")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Clearly invalid indexed entries spotted: **{len(INVALID_INDEXED)}**")
    lines.append(f"- Clearly missing research threads spotted: **{len(MISSING_RESEARCH)}**")
    lines.append(f"- Broken/stale links spotted: **{len(broken_links)}**")
    lines.append("")
    lines.append("## Likely Failure Modes")
    lines.append("")
    lines.append("### 1. Keyword Leakage Into Non-Research Chats")
    lines.append("")
    lines.append("The generator appears to rely too heavily on broad phrases like `deep research`, `research plan`, `research allowance`, `searching the web`, or `research tool` without verifying whether the conversation actually executed a research run.")
    lines.append("")
    lines.append("This likely caused prompt-writing, translations, meta-tool discussions, and plan-only threads to get indexed as if they were completed research reports.")
    lines.append("")
    lines.append("Examples:")
    lines.append("- ChatGPT meta/tooling entries such as `20260220_chatgpt-deep-research-limits.md` and `20251015_mastering-deep-research.md`.")
    lines.append("- Claude tool-guide/meta entries such as `20251014_claude-research-tool-guide.md` and `20251214_using-research-allowance-effectively.md`.")
    lines.append("- Gemini prompt/template entries such as `20260130_create-a-prompt-to-think-and-research-ho.md` and `20250802_create-meta-prompt-instruct.md`.")
    lines.append("")
    lines.append("### 2. Prompt/Planning Threads Were Not Separated From Execution Threads")
    lines.append("")
    lines.append("Several indexed files are clearly pre-execution scaffolding: validation prompts, clarification prompts, or protocol drafts. The current logic appears to treat the presence of research language as enough evidence, even when the thread never reaches actual source gathering and synthesis.")
    lines.append("")
    lines.append("Examples:")
    lines.append("- `20251024_research-plan-validation.md`")
    lines.append("- `20251024_research-task-clarification.md`")
    lines.append("- `20251024_research-plan-clarification.md`")
    lines.append("- `20251024_research-protocol-clarification.md`")
    lines.append("")
    lines.append("### 3. Translations And Reformattings Were Indexed As New Research")
    lines.append("")
    lines.append("A few entries are plainly derivative transformations of earlier reports rather than original research runs. This suggests the current indexing logic does not check whether the user is asking for new research versus manipulating existing material.")
    lines.append("")
    lines.append("Examples:")
    lines.append("- `20260226_wolf-buddha-ζωή.md`")
    lines.append("- `20260121_μετάφραση-και-βελτιστοποίηση-tts.md`")
    lines.append("- `20251030_translate-to-greek.md`")
    lines.append("- `20250912_translate-the-deep-research-report-to-gr.md`")
    lines.append("")
    lines.append("### 4. Internal Analysis Was Confused With External Research")
    lines.append("")
    lines.append("Some Claude entries were included even though they are really project-memory analysis, chat indexing, or workflow planning, not web research or research-tool output. This likely happened because the conversation contains words like `research`, `search`, or `analyze`, but the object of analysis is local/project material.")
    lines.append("")
    lines.append("Examples:")
    lines.append("- `20251120_project-chat-index-with-descriptions.md`")
    lines.append("- `20251120_chat-analysis-and-prioritization-index.md`")
    lines.append("- `20251225_x3lixios-obsidian-integration-and-workfl.md`")
    lines.append("")
    lines.append("### 5. Missing Threads Point To Under-Matching Of Strong Research Artifacts")
    lines.append("")
    lines.append("The current detector also misses genuine research threads when they do not use the exact trigger phrases it expects, or when the evidence is mostly in a large final report rather than in repeated `research` wording.")
    lines.append("")
    lines.append("Examples:")
    lines.append("- ChatGPT: `20251204_deepstash-app-reconstruction-plan.md`, `20251108_micro-learning-app-comparison.md`, `20251019_bromantane-research-analysis.md`")
    lines.append("- Claude: `20260214_evidence-based-longevity-biohacks-ranked.md`, `20260116_current-llm-market-share-breakdown.md`, `20251130_claude-flow-swe-bench-benchmark-verifica.md`")
    lines.append("- Gemini: `20260213_deepstash-rebuild-reference.md`, `20251108_messaging-platform-data-capture-research.md`, `20251030_interactive-competitive-analysis.md`")
    lines.append("")
    lines.append("### 6. Filename Drift Created At Least One Stale Link")
    lines.append("")
    lines.append("The current index contains at least one dead Gemini link. That usually means the conversation file was renamed or regenerated after the index was built, but the link table was not refreshed.")
    lines.append("")
    if broken_links:
        for rel_path in broken_links:
            lines.append(f"- Broken link present in the index: `{rel_path}`")
    else:
        lines.append("- No broken links were detected in this pass.")
    lines.append("")
    lines.append("### 7. Mixed Artifact Threads Need Better Source-Specific Rules")
    lines.append("")
    lines.append("Gemini is especially prone to mixed-mode outputs: prompt drafts, Canvas reports, HTML artifacts, and design mockups all sit in the same export format. The current indexing logic seems too simple to distinguish a real Deep Research run from a general Canvas artifact.")
    lines.append("")
    lines.append("The same source-specific issue exists for Claude, where `This block is not supported on your current device yet.` often signals tool activity, but not all tool activity is web research.")
    lines.append("")
    lines.append("## What The Indexer Probably Needs")
    lines.append("")
    lines.append("- Source-specific validation rules instead of one broad regex bucket.")
    lines.append("- A post-match verification step that checks for actual execution evidence, not just research-like wording.")
    lines.append("- A distinction between plan/clarification/prompt-writing threads and final report threads.")
    lines.append("- A link-existence check before writing the markdown index.")
    lines.append("- Better handling for Canvas/HTML artifact threads in Gemini exports.")
    lines.append("")
    lines.append("## Invalid Entry Count By Source")
    lines.append("")
    for source in ("ChatGPT", "Claude", "Gemini"):
        lines.append(f"- **{source}**: {source_invalid_counts[source]}")
    lines.append("")
    return "\n".join(lines)


def build_rerun_report(corrected_links: set[str]) -> str:
    conversations = [load_conversation(rel_path) for rel_path in corrected_links]
    conversations.sort(key=lambda conv: (conv.date, conv.source, conv.title.lower()), reverse=True)

    grouped: dict[str, list[Conversation]] = {"ChatGPT": [], "Claude": [], "Gemini": []}
    for conv in conversations:
        grouped[conv.source].append(conv)

    lines: list[str] = []
    lines.append("# Validated Research Threads: Rerun Grades")
    lines.append("")
    lines.append("High-confidence corrected corpus derived from `[RESEARCH_INDEX.md](RESEARCH_INDEX.md)` plus clear missing research threads.")
    lines.append("")
    lines.append("## Scoring")
    lines.append("")
    lines.append("- `9-10`: highly valuable, still relevant, and especially worth rerunning with stronger 2026 models")
    lines.append("- `7-8`: clearly useful and rerunnable, but narrower or more time-bound")
    lines.append("- `5-6`: some value, though more situational, dated, or lower leverage")
    lines.append("- `1-4`: weak rerun candidate even if it still qualified as a research thread")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    for source in ("ChatGPT", "Claude", "Gemini"):
        lines.append(f"- **{source}**: {len(grouped[source])} validated threads")
    lines.append("")

    for source in ("ChatGPT", "Claude", "Gemini"):
        lines.append(f"## {source}")
        lines.append("")
        lines.append("| Date | Grade | Conversation | Research Prompt / Query |")
        lines.append("|------|-------|--------------|--------------------------|")
        for conv in grouped[source]:
            score = score_conversation(conv)
            title = clean_table_text(conv.title, limit=90)
            query = clean_table_text(conv.first_query or conv.title, limit=220)
            lines.append(
                f"| {conv.date} | {score}/10 | "
                f"[{title}]({conv.rel_path}) | {query} |"
            )
        lines.append("")

    return "\n".join(lines)


def build_fixed_report(indexed_links: set[str], corrected_links: set[str]) -> str:
    conversations = [load_conversation(rel_path) for rel_path in corrected_links]
    conversations.sort(key=lambda conv: (conv.date, conv.source, conv.title.lower()), reverse=True)

    grouped: dict[str, list[Conversation]] = {"ChatGPT": [], "Claude": [], "Gemini": []}
    for conv in conversations:
        grouped[conv.source].append(conv)

    lines: list[str] = []
    lines.append("# Research Index Fixed Report")
    lines.append("")
    lines.append("This is the corrected, diff-friendly replacement report derived from the current `RESEARCH_INDEX.md` plus a conservative validation pass.")
    lines.append("")
    lines.append("## Fix Summary")
    lines.append("")
    lines.append(f"- Original indexed links: **{len(indexed_links)}**")
    lines.append(f"- Removed clear false positives: **{len(INVALID_INDEXED)}**")
    lines.append(f"- Added clear missing research threads: **{len(MISSING_RESEARCH)}**")
    lines.append("- Removed broken/stale Gemini link: **`gemini/conversations/20260302_start-research.md`**")
    lines.append(f"- Corrected validated corpus: **{len(corrected_links)}**")
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- This report keeps only high-confidence research threads.")
    lines.append("- Prompt-writing, translations, tooling/meta chats, and plan-only threads were excluded.")
    lines.append("- The score estimates rerun-worthiness in 2026 based on usefulness, continuing relevance, and whether stronger current models would likely improve the output.")
    lines.append("")

    for source in ("ChatGPT", "Claude", "Gemini"):
        lines.append(f"## {source}")
        lines.append("")
        lines.append(f"Validated threads: **{len(grouped[source])}**")
        lines.append("")
        lines.append("| Date | Grade | Conversation | Research Prompt / Query |")
        lines.append("|------|-------|--------------|--------------------------|")
        for conv in grouped[source]:
            score = score_conversation(conv)
            title = clean_table_text(conv.title, limit=90)
            query = clean_table_text(conv.first_query or conv.title, limit=220)
            lines.append(
                f"| {conv.date} | {score}/10 | "
                f"[{title}]({conv.rel_path}) | {query} |"
            )
        lines.append("")

    lines.append("## Companion Audit Files")
    lines.append("")
    lines.append(f"- Validation report: `[{VALIDATION_PATH.name}]({VALIDATION_PATH.name})`")
    lines.append(f"- Error report: `[{ERROR_REPORT_PATH.name}]({ERROR_REPORT_PATH.name})`")
    lines.append(f"- Rerun grades report: `[{RERUN_PATH.name}]({RERUN_PATH.name})`")
    lines.append("")
    return "\n".join(lines)


def build_strict_fixed_report(indexed_links: set[str], corrected_links: set[str], strict_links: set[str]) -> str:
    conversations = [load_conversation(rel_path) for rel_path in strict_links]
    conversations.sort(key=lambda conv: (conv.date, conv.source, conv.title.lower()), reverse=True)

    grouped: dict[str, list[Conversation]] = {"ChatGPT": [], "Claude": [], "Gemini": []}
    for conv in conversations:
        grouped[conv.source].append(conv)

    lines: list[str] = []
    lines.append("# Research Index Fixed Report: Strict Executed Deep Research Only")
    lines.append("")
    lines.append("This narrower report keeps only conversations with stronger evidence that a Deep Research style run was actually executed, not merely discussed or approximated.")
    lines.append("")
    lines.append("## Strict Filter")
    lines.append("")
    lines.append("- **ChatGPT**: kept only `model: research` threads or threads with a clear research-task kickoff artifact plus a substantial final report.")
    lines.append("- **Claude**: kept only threads with visible research-tool activity plus an explicitly external web-research style prompt.")
    lines.append("- **Gemini**: kept only Canvas reports with explicit research-plan/execute/complete lifecycle signals.")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Original indexed links: **{len(indexed_links)}**")
    lines.append(f"- High-confidence corrected research corpus: **{len(corrected_links)}**")
    lines.append(f"- Strict executed deep-research corpus: **{len(strict_links)}**")
    for source in ("ChatGPT", "Claude", "Gemini"):
        lines.append(f"- **{source}**: {len(grouped[source])}")
    lines.append("")

    for source in ("ChatGPT", "Claude", "Gemini"):
        lines.append(f"## {source}")
        lines.append("")
        lines.append(f"Validated executed deep-research threads: **{len(grouped[source])}**")
        lines.append("")
        lines.append("| Date | Grade | Conversation | Research Prompt / Query |")
        lines.append("|------|-------|--------------|--------------------------|")
        for conv in grouped[source]:
            score = score_conversation(conv)
            title = clean_table_text(conv.title, limit=90)
            query = clean_table_text(conv.first_query or conv.title, limit=220)
            lines.append(
                f"| {conv.date} | {score}/10 | "
                f"[{title}]({conv.rel_path}) | {query} |"
            )
        lines.append("")

    lines.append("## Related Files")
    lines.append("")
    lines.append(f"- Broad corrected report: `[{FIXED_REPORT_PATH.name}]({FIXED_REPORT_PATH.name})`")
    lines.append(f"- Validation audit: `[{VALIDATION_PATH.name}]({VALIDATION_PATH.name})`")
    lines.append(f"- Error report: `[{ERROR_REPORT_PATH.name}]({ERROR_REPORT_PATH.name})`")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    indexed_links = collect_index_links()
    corrected_links = build_corrected_set(indexed_links)
    strict_links = build_strict_executed_set(corrected_links)

    VALIDATION_PATH.write_text(
        build_validation_report(indexed_links, corrected_links),
        encoding="utf-8",
    )
    RERUN_PATH.write_text(
        build_rerun_report(corrected_links),
        encoding="utf-8",
    )
    ERROR_REPORT_PATH.write_text(
        build_error_report(indexed_links),
        encoding="utf-8",
    )
    FIXED_REPORT_PATH.write_text(
        build_fixed_report(indexed_links, corrected_links),
        encoding="utf-8",
    )
    STRICT_FIXED_REPORT_PATH.write_text(
        build_strict_fixed_report(indexed_links, corrected_links, strict_links),
        encoding="utf-8",
    )

    print(f"Wrote {VALIDATION_PATH}")
    print(f"Wrote {RERUN_PATH}")
    print(f"Wrote {ERROR_REPORT_PATH}")
    print(f"Wrote {FIXED_REPORT_PATH}")
    print(f"Wrote {STRICT_FIXED_REPORT_PATH}")


if __name__ == "__main__":
    main()
