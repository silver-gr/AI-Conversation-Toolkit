#!/usr/bin/env -S uv run --quiet --script
# /// script
# dependencies = [
#   "rich",
#   "httpx",
# ]
# requires-python = ">=3.11"
# ///

"""
Biographical Profile Extractor v2 - OpenAI-Compatible API
=========================================================

Extracts biographical information from AI conversation markdown files
using any OpenAI-compatible endpoint (agy/Gemini proxy, NVIDIA NIM,
Ollama Cloud, etc.) via direct HTTP — no CLI tools required.

Adaptive context loading: with large-context models (Gemini 1M+),
the full conversation is sent. The content budget adapts based on
the model's context window and truncation feedback.

Usage:
    # Default: agy provider (local Gemini proxy)
    uv run scripts/biography_extractor_v2.py --all-sources

    # Custom endpoint
    uv run scripts/biography_extractor_v2.py --all-sources \\
        --base-url https://integrate.api.nvidia.com/v1 \\
        --api-key nvapi-xxx --model deepseek-ai/deepseek-v4-flash

    # Sample mode (test with N files)
    uv run scripts/biography_extractor_v2.py --all-sources --sample 5

    # Export extractions to JSON
    uv run scripts/biography_extractor_v2.py --all-sources \\
        --export output/profile/data/extractions.json

    # Export all cached extractions (no API calls)
    uv run scripts/biography_extractor_v2.py --export-cached
"""

import json
import argparse
import hashlib
import sqlite3
import pickle
import re
import os
import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, BarColumn, TaskProgressColumn
from rich.table import Table

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

console = Console()

CONVERSATION_DIRS = [
    "output/chatgpt-full/conversations",
    "output/claude-full/conversations",
    "output/gemini/conversations",
]

# Defaults: agy proxy (local Gemini endpoint, OpenAI-compatible)
DEFAULT_BASE_URL = "http://127.0.0.1:8317/v1"
DEFAULT_API_KEY = "sk-rT24hZlNDy2cbLvF6oPQcklYeESDyouT"
DEFAULT_MODEL = "gemini-3-flash-preview"

CONCURRENCY = 3
BATCH_SIZE = 5
RETRY_ATTEMPTS = 3
RETRY_DELAY = 5.0
CALL_SPACING = 0.5

# Per-file context budget for single-file fallback.
SINGLE_FILE_BUDGET = 50_000

SYSTEM_PROMPT = (
    "You are extracting biographical information about the USER from an AI "
    "conversation. Focus on learning about the person who is asking questions "
    "or making requests. Output valid JSON only — no markdown, no explanation, "
    "no code blocks."
)

EXTRACTION_SCHEMA = """{
    "identity": {
        "name_mentions": [],
        "location_mentions": [],
        "language_indicators": [],
        "demographic_hints": []
    },
    "work": {
        "occupation_hints": [],
        "projects_mentioned": [],
        "skills_demonstrated": [],
        "business_ventures": [],
        "work_style": []
    },
    "health": {
        "physical_health": [],
        "mental_health": [],
        "sleep_patterns": [],
        "nutrition_diet": [],
        "substances": [],
        "medications": []
    },
    "relationships": {
        "romantic": [],
        "family": [],
        "social": [],
        "sexuality": []
    },
    "interests": {
        "hobbies": [],
        "intellectual": [],
        "entertainment": [],
        "creative": []
    },
    "goals": {
        "short_term": [],
        "long_term": [],
        "dreams": [],
        "fears": []
    },
    "challenges": {
        "current_problems": [],
        "recurring_patterns": [],
        "blockers": [],
        "seeking_help_for": []
    },
    "daily_life": {
        "routines": [],
        "tools_apps": [],
        "living_situation": [],
        "finances": []
    },
    "values": {
        "explicit_values": [],
        "implicit_values": [],
        "philosophical": [],
        "political_social": []
    },
    "context": {
        "main_topic": "",
        "emotional_tone": "",
        "biographical_richness": ""
    }
}"""


def _truncate_content(content: str, budget: int) -> str:
    """Truncate content to fit within a character budget.

    For budgets large enough to hold the full content, returns as-is.
    Otherwise takes the first half and last half of the budget, joined
    by a marker so the model knows content was omitted.
    """
    if len(content) <= budget:
        return content
    half = budget // 2
    return content[:half] + "\n\n[... middle content omitted for context budget ...]\n\n" + content[-half:]


def _strip_markdown_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        first_newline = text.find("\n")
        if first_newline != -1:
            text = text[first_newline + 1:]
        if text.endswith("```"):
            text = text[:-3].rstrip()
    return text


def _parse_json_from_text(text: str) -> dict:
    """Extract JSON from a text response, handling markdown fences and comments."""
    text = _strip_markdown_fences(text)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    json_start = text.find("{")
    json_end = text.rfind("}") + 1
    if json_start != -1 and json_end > json_start:
        json_str = text[json_start:json_end]
        json_str = re.sub(r"//.*$", "", json_str, flags=re.MULTILINE)
        json_str = re.sub(r"/\*.*?\*/", "", json_str, flags=re.DOTALL)
        return json.loads(json_str)

    raise ValueError("No valid JSON found in response")


class BiographyExtractorV2:
    def __init__(
        self,
        cache_file: str = "biography_cache.db",
        base_url: str = DEFAULT_BASE_URL,
        api_key: str = DEFAULT_API_KEY,
        model: str = DEFAULT_MODEL,
    ):
        self.cache_file = cache_file
        self.cache_hits = 0
        self.cache_misses = 0
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.truncation_count = 0
        import threading
        self._lock = threading.Lock()
        self._init_cache()

        console.print(f"[cyan]Endpoint: {self.base_url} | Model: {self.model}[/cyan]")

    def _get_connection(self):
        return sqlite3.connect(self.cache_file)

    def _init_cache(self):
        conn = self._get_connection()
        conn.execute("PRAGMA journal_mode=WAL")
        cursor = conn.cursor()

        cursor.execute("""
            CREATE TABLE IF NOT EXISTS biography_cache (
                idempotency_key TEXT PRIMARY KEY,
                file_path TEXT,
                content_hash TEXT,
                extraction_data BLOB,
                created_at TEXT,
                source TEXT,
                conversation_date TEXT,
                provider TEXT
            )
        """)
        cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_file_path
            ON biography_cache(file_path)
        """)
        conn.commit()

        cursor.execute("SELECT COUNT(*) FROM biography_cache")
        cache_count = cursor.fetchone()[0]
        if cache_count > 0:
            console.print(f"[blue]Cache: {cache_count} existing extractions[/blue]")
        conn.close()

    def _generate_idempotency_key(self, file_path: str, content: str) -> str:
        content_hash = hashlib.sha256(content.encode()).hexdigest()
        combined = f"{file_path}|{content_hash}"
        return hashlib.sha256(combined.encode()).hexdigest()

    def _get_cached_extraction(self, idempotency_key: str) -> Optional[Dict]:
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                "SELECT extraction_data FROM biography_cache WHERE idempotency_key = ?",
                (idempotency_key,),
            )
            result = cursor.fetchone()
            conn.close()
        except Exception as e:
            logger.warning("Cache read failed: %s", e)
            return None

        if result:
            with self._lock:
                self.cache_hits += 1
            return pickle.loads(result[0])

        with self._lock:
            self.cache_misses += 1
        return None

    def _save_to_cache(
        self,
        idempotency_key: str,
        file_path: str,
        content_hash: str,
        extraction_data: Dict,
        source: str,
        conversation_date: str,
    ):
        try:
            conn = self._get_connection()
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT OR REPLACE INTO biography_cache
                (idempotency_key, file_path, content_hash, extraction_data,
                 created_at, source, conversation_date, provider)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    idempotency_key,
                    file_path,
                    content_hash,
                    pickle.dumps(extraction_data),
                    datetime.now().isoformat(),
                    source,
                    conversation_date,
                    f"{self.base_url}|{self.model}",
                ),
            )
            conn.commit()
            conn.close()
        except Exception as e:
            logger.warning("Cache write failed: %s", e)

    def discover_conversations(self, directories: List[str]) -> List[Tuple[str, str]]:
        conversations = []
        for directory in directories:
            dir_path = Path(directory)
            if not dir_path.exists():
                console.print(f"[yellow]Directory not found: {directory}[/yellow]")
                continue

            if "chatgpt" in directory.lower():
                source = "chatgpt"
            elif "claude" in directory.lower():
                source = "claude"
            elif "gemini" in directory.lower():
                source = "gemini"
            else:
                source = "unknown"

            md_files = [
                f for f in dir_path.glob("*.md")
                if not f.name.upper().startswith("INDEX")
            ]
            for md_file in md_files:
                conversations.append((str(md_file), source))

        conversations.sort(key=lambda x: Path(x[0]).name)
        return conversations

    def extract_date_from_filename(self, filename: str) -> str:
        name = Path(filename).stem
        match = re.match(r"(\d{8})", name)
        if match:
            try:
                date = datetime.strptime(match.group(1), "%Y%m%d")
                return date.strftime("%Y-%m-%d")
            except ValueError:
                pass
        return "unknown"

    async def _call_llm(
        self,
        client: httpx.AsyncClient,
        messages: list[dict],
    ) -> tuple[str, bool]:
        """Call OpenAI-compatible chat/completions. Returns (content, was_truncated)."""
        for attempt in range(RETRY_ATTEMPTS):
            try:
                resp = await client.post(
                    f"{self.base_url}/chat/completions",
                    json={
                        "model": self.model,
                        "messages": messages,
                        "temperature": 0.2,
                    },
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                    timeout=300.0,
                )
                if resp.status_code in (429, 500, 502, 503):
                    wait = RETRY_DELAY * (2 ** attempt)
                    logger.warning("Server %d, waiting %.1fs (attempt %d/%d)...",
                                   resp.status_code, wait, attempt + 1, RETRY_ATTEMPTS)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                data = resp.json()
                choice = data["choices"][0]
                content = choice["message"]["content"]
                truncated = choice.get("finish_reason") == "length"
                await asyncio.sleep(CALL_SPACING)
                return content, truncated
            except (httpx.HTTPStatusError, httpx.ReadTimeout, KeyError) as e:
                if attempt < RETRY_ATTEMPTS - 1:
                    wait = RETRY_DELAY * (2 ** attempt)
                    logger.warning("Attempt %d failed (%s), retrying in %.1fs...",
                                   attempt + 1, e, wait)
                    await asyncio.sleep(wait)
                else:
                    raise
        raise RuntimeError("All retry attempts exhausted")

    def _build_batch_messages(self, items: list[dict]) -> list[dict]:
        """Build a prompt that extracts from multiple conversations at once."""
        conversations_block = ""
        for i, item in enumerate(items, 1):
            conversations_block += (
                f"\n{'='*40}\n"
                f"CONVERSATION {i} OF {len(items)}\n"
                f"SOURCE: {item['source'].upper()} | DATE: {item['date']}\n"
                f"{'='*40}\n"
                f"{item['content']}\n"
            )

        user_prompt = f"""EXTRACTION SCHEMA (per conversation):
{EXTRACTION_SCHEMA}

RULES:
- Focus ONLY on information about the USER (the human asking questions)
- Extract both explicit statements AND reasonable inferences
- Use empty arrays [] when no relevant information found
- Be specific: "learning React" not just "programming"
- "biographical_richness": rate as "high", "medium", "low", or "minimal"

You have {len(items)} conversations below. Output a JSON ARRAY of {len(items)} objects,
one per conversation, in the SAME ORDER. No other text.

{conversations_block}

JSON ARRAY OUTPUT:"""
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

    def _build_single_messages(self, content: str, source: str, date: str) -> list[dict]:
        user_prompt = f"""EXTRACTION SCHEMA:
{EXTRACTION_SCHEMA}

RULES:
- Focus ONLY on information about the USER (the human asking questions)
- Extract both explicit statements AND reasonable inferences
- Use empty arrays [] when no relevant information found
- Be specific: "learning React" not just "programming"
- "biographical_richness": rate as "high", "medium", "low", or "minimal"

SOURCE: {source.upper()}
DATE: {date}

CONVERSATION:
{content}

JSON OUTPUT:"""
        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]

    async def _extract_single(
        self, client: httpx.AsyncClient, item: dict,
    ) -> Dict:
        """Fallback: extract one conversation individually."""
        content = _truncate_content(item["full_content"], SINGLE_FILE_BUDGET)
        messages = self._build_single_messages(content, item["source"], item["date"])
        try:
            raw, truncated = await self._call_llm(client, messages)
            if truncated:
                with self._lock:
                    self.truncation_count += 1
                logger.warning("%s truncated even solo", Path(item["file_path"]).name)
                return self._empty_extraction(item["file_path"], item["source"])
            parsed = _parse_json_from_text(raw)
            return self._clean_extraction(parsed)
        except Exception as e:
            logger.warning("Solo extraction failed for %s: %s", Path(item["file_path"]).name, e)
            return self._empty_extraction(item["file_path"], item["source"])

    async def _extract_batch(
        self,
        client: httpx.AsyncClient,
        semaphore: asyncio.Semaphore,
        batch: list[dict],
        batch_num: int,
        total_batches: int,
    ) -> list[Dict]:
        """Extract biographical data from a batch of conversations in one LLM call.

        Falls back to individual extraction on truncation or parse failure.
        """
        async with semaphore:
            logger.info("Batch %d/%d (%d files)...", batch_num, total_batches, len(batch))

            # Build batch prompt with truncated content per file
            per_file_budget = 800_000 // max(len(batch), 1)
            for item in batch:
                item["content"] = _truncate_content(item["full_content"], per_file_budget)

            messages = self._build_batch_messages(batch)

            try:
                raw, truncated = await self._call_llm(client, messages)
            except Exception as e:
                logger.warning("Batch %d LLM call failed (%s), falling back to individual...", batch_num, e)
                results = []
                for item in batch:
                    results.append(await self._extract_single(client, item))
                return results

            if truncated:
                with self._lock:
                    self.truncation_count += 1
                logger.warning("Batch %d truncated, falling back to individual...", batch_num)
                results = []
                for item in batch:
                    results.append(await self._extract_single(client, item))
                return results

            # Parse the JSON array
            try:
                parsed = _parse_json_from_text(raw)
            except (json.JSONDecodeError, ValueError):
                logger.warning("Batch %d JSON parse failed, falling back to individual...", batch_num)
                results = []
                for item in batch:
                    results.append(await self._extract_single(client, item))
                return results

            if not isinstance(parsed, list) or len(parsed) != len(batch):
                logger.warning(
                    "Batch %d returned %d items (expected %d), falling back...",
                    batch_num, len(parsed) if isinstance(parsed, list) else 0, len(batch),
                )
                results = []
                for item in batch:
                    results.append(await self._extract_single(client, item))
                return results

            return [self._clean_extraction(p) for p in parsed]

    def _clean_extraction(self, extracted: Dict) -> Dict:
        cleaned = {}
        categories = {
            "identity": ["name_mentions", "location_mentions", "language_indicators", "demographic_hints"],
            "work": ["occupation_hints", "projects_mentioned", "skills_demonstrated", "business_ventures", "work_style"],
            "health": ["physical_health", "mental_health", "sleep_patterns", "nutrition_diet", "substances", "medications"],
            "relationships": ["romantic", "family", "social", "sexuality"],
            "interests": ["hobbies", "intellectual", "entertainment", "creative"],
            "goals": ["short_term", "long_term", "dreams", "fears"],
            "challenges": ["current_problems", "recurring_patterns", "blockers", "seeking_help_for"],
            "daily_life": ["routines", "tools_apps", "living_situation", "finances"],
            "values": ["explicit_values", "implicit_values", "philosophical", "political_social"],
            "context": ["main_topic", "emotional_tone", "biographical_richness"],
        }
        string_fields = {"main_topic", "emotional_tone", "biographical_richness"}

        for category, fields in categories.items():
            cleaned[category] = {}
            cat_data = extracted.get(category, {})
            if not isinstance(cat_data, dict):
                cat_data = {}

            for field in fields:
                default = "" if field in string_fields else []
                value = cat_data.get(field, default)

                if isinstance(value, list):
                    cleaned[category][field] = [
                        str(item).strip()
                        for item in value
                        if item and str(item).strip()
                    ][:10]
                else:
                    cleaned[category][field] = str(value).strip() if value else ""

        return cleaned

    def _empty_extraction_data(self) -> Dict:
        return {
            "identity": {"name_mentions": [], "location_mentions": [], "language_indicators": [], "demographic_hints": []},
            "work": {"occupation_hints": [], "projects_mentioned": [], "skills_demonstrated": [], "business_ventures": [], "work_style": []},
            "health": {"physical_health": [], "mental_health": [], "sleep_patterns": [], "nutrition_diet": [], "substances": [], "medications": []},
            "relationships": {"romantic": [], "family": [], "social": [], "sexuality": []},
            "interests": {"hobbies": [], "intellectual": [], "entertainment": [], "creative": []},
            "goals": {"short_term": [], "long_term": [], "dreams": [], "fears": []},
            "challenges": {"current_problems": [], "recurring_patterns": [], "blockers": [], "seeking_help_for": []},
            "daily_life": {"routines": [], "tools_apps": [], "living_situation": [], "finances": []},
            "values": {"explicit_values": [], "implicit_values": [], "philosophical": [], "political_social": []},
            "context": {"main_topic": "", "emotional_tone": "", "biographical_richness": "minimal"},
        }

    def _empty_extraction(self, file_path: str, source: str) -> Dict:
        extraction = self._empty_extraction_data()
        extraction["_metadata"] = {
            "file_path": file_path,
            "source": source,
            "conversation_date": self.extract_date_from_filename(file_path),
            "extracted_at": datetime.now().isoformat(),
            "content_length": 0,
            "error": True,
            "provider": f"{self.base_url}|{self.model}",
        }
        return extraction

    async def process_all_async(
        self, conversations: List[Tuple[str, str]],
    ) -> List[Dict]:
        """Process conversations: serve from cache, batch uncached via LLM.

        Groups uncached conversations into batches of BATCH_SIZE and sends
        each batch as one LLM call. Falls back to individual on failure.
        """
        all_extractions: dict[str, Dict] = {}  # keyed by file_path for ordering
        uncached_items: list[dict] = []

        # Phase 1: check cache, collect uncached
        for file_path, source in conversations:
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    full_content = f.read()
            except Exception as e:
                logger.error("Failed to read %s: %s", file_path, e)
                all_extractions[file_path] = self._empty_extraction(file_path, source)
                continue

            idempotency_key = self._generate_idempotency_key(file_path, full_content)
            cached = self._get_cached_extraction(idempotency_key)
            if cached:
                all_extractions[file_path] = cached
            else:
                uncached_items.append({
                    "file_path": file_path,
                    "source": source,
                    "full_content": full_content,
                    "content_hash": hashlib.sha256(full_content.encode()).hexdigest(),
                    "idempotency_key": idempotency_key,
                    "date": self.extract_date_from_filename(file_path),
                })

        console.print(
            f"[blue]Cache: {len(all_extractions)} hits, "
            f"{len(uncached_items)} to extract in ~{(len(uncached_items) + BATCH_SIZE - 1) // BATCH_SIZE} batches[/blue]"
        )

        if not uncached_items:
            return [all_extractions[fp] for fp, _ in conversations if fp in all_extractions]

        # Phase 2: batch uncached into LLM calls
        batches = [
            uncached_items[i:i + BATCH_SIZE]
            for i in range(0, len(uncached_items), BATCH_SIZE)
        ]
        semaphore = asyncio.Semaphore(CONCURRENCY)

        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TaskProgressColumn(),
            console=console,
        ) as progress:
            task = progress.add_task(
                f"[cyan]Extracting via {self.model} ({len(batches)} batches)...",
                total=len(uncached_items),
            )

            async with httpx.AsyncClient() as client:
                async def _process_batch(batch: list[dict], batch_num: int):
                    results = await self._extract_batch(
                        client, semaphore, batch, batch_num, len(batches),
                    )
                    for item, extraction in zip(batch, results):
                        extraction["_metadata"] = {
                            "file_path": item["file_path"],
                            "source": item["source"],
                            "conversation_date": item["date"],
                            "extracted_at": datetime.now().isoformat(),
                            "content_length": len(item["full_content"]),
                            "provider": f"{self.base_url}|{self.model}",
                        }
                        self._save_to_cache(
                            item["idempotency_key"], item["file_path"],
                            item["content_hash"], extraction,
                            item["source"], item["date"],
                        )
                        all_extractions[item["file_path"]] = extraction
                        progress.advance(task)

                batch_tasks = [
                    _process_batch(batch, i + 1)
                    for i, batch in enumerate(batches)
                ]
                await asyncio.gather(*batch_tasks, return_exceptions=True)

        return [all_extractions.get(fp, self._empty_extraction(fp, src)) for fp, src in conversations]

    def print_statistics(self, extractions: List[Dict]):
        console.print()

        table = Table(title=f"Extraction Statistics ({self.model})")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Total conversations", str(len(extractions)))
        table.add_row("Cache hits", str(self.cache_hits))
        table.add_row("Cache misses (API calls)", str(self.cache_misses))
        table.add_row("Truncation retries", str(self.truncation_count))

        if self.cache_hits + self.cache_misses > 0:
            hit_rate = self.cache_hits / (self.cache_hits + self.cache_misses) * 100
            table.add_row("Cache hit rate", f"{hit_rate:.1f}%")

        richness_counts = {"high": 0, "medium": 0, "low": 0, "minimal": 0}
        for ext in extractions:
            richness = ext.get("context", {}).get("biographical_richness", "minimal").lower()
            richness_counts[richness] = richness_counts.get(richness, 0) + 1

        table.add_row("High richness", str(richness_counts["high"]))
        table.add_row("Medium richness", str(richness_counts["medium"]))
        table.add_row("Low richness", str(richness_counts["low"]))
        table.add_row("Minimal richness", str(richness_counts["minimal"]))

        console.print(table)

        source_counts: dict[str, int] = {}
        for ext in extractions:
            source = ext.get("_metadata", {}).get("source", "unknown")
            source_counts[source] = source_counts.get(source, 0) + 1
        console.print("\n[bold]By Source:[/bold]")
        for source, count in sorted(source_counts.items()):
            console.print(f"  {source}: {count}")

    def export_to_json(self, extractions: List[Dict], output_file: str):
        Path(output_file).parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, "w", encoding="utf-8") as f:
            json.dump(extractions, f, indent=2, ensure_ascii=False)
        console.print(f"[green]Exported {len(extractions)} extractions to {output_file}[/green]")

    def get_all_cached_extractions(self) -> List[Dict]:
        conn = self._get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "SELECT extraction_data FROM biography_cache ORDER BY conversation_date"
        )
        results = cursor.fetchall()
        conn.close()
        return [pickle.loads(row[0]) for row in results]


async def main():
    parser = argparse.ArgumentParser(
        description="Extract biographical information via OpenAI-compatible API",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Environment variables (override CLI defaults):
  BIO_BASE_URL   API base URL (default: http://127.0.0.1:8317/v1)
  BIO_API_KEY    API key
  BIO_MODEL      Model name (default: gemini-3-flash-preview)

Examples:
  uv run scripts/biography_extractor_v2.py --all-sources
  uv run scripts/biography_extractor_v2.py --all-sources --sample 5
  uv run scripts/biography_extractor_v2.py --export-cached --export output/profile/data/extractions.json
        """,
    )
    parser.add_argument("directory", nargs="?", help="Directory with conversation markdown files")
    parser.add_argument("--all-sources", action="store_true", help="Process all conversation sources")
    parser.add_argument("--sample", type=int, help="Only process N random conversations")
    parser.add_argument("--export", type=str, help="Export extractions to JSON file")
    parser.add_argument("--cache-file", type=str, default="biography_cache.db", help="Cache database file")
    parser.add_argument("--export-cached", action="store_true", help="Export all cached extractions (no API calls)")
    parser.add_argument("--base-url", type=str,
                        default=os.environ.get("BIO_BASE_URL", DEFAULT_BASE_URL),
                        help="OpenAI-compatible API base URL")
    parser.add_argument("--api-key", type=str,
                        default=os.environ.get("BIO_API_KEY", DEFAULT_API_KEY),
                        help="API key for the endpoint")
    parser.add_argument("--model", type=str,
                        default=os.environ.get("BIO_MODEL", DEFAULT_MODEL),
                        help="Model name to use")

    args = parser.parse_args()

    extractor = BiographyExtractorV2(
        cache_file=args.cache_file,
        base_url=args.base_url,
        api_key=args.api_key,
        model=args.model,
    )

    if args.export_cached:
        extractions = extractor.get_all_cached_extractions()
        output_file = args.export or "output/profile/data/extractions.json"
        extractor.export_to_json(extractions, output_file)
        return

    if args.all_sources:
        directories = CONVERSATION_DIRS
    elif args.directory:
        directories = [args.directory]
    else:
        console.print("[red]Please specify a directory or use --all-sources[/red]")
        return

    console.print("[cyan]Discovering conversation files...[/cyan]")
    conversations = extractor.discover_conversations(directories)
    console.print(f"[green]Found {len(conversations)} conversation files[/green]")

    if not conversations:
        console.print("[yellow]No conversations found[/yellow]")
        return

    if args.sample:
        import random
        conversations = random.sample(conversations, min(args.sample, len(conversations)))
        console.print(f"[blue]Sampling {len(conversations)} conversations[/blue]")

    extractions = await extractor.process_all_async(conversations)
    extractor.print_statistics(extractions)

    if args.export:
        extractor.export_to_json(extractions, args.export)


if __name__ == "__main__":
    asyncio.run(main())
