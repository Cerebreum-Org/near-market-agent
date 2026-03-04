"""Deep research phase — runs before the builder agent.

Analyzes the job description, identifies technologies/APIs/packages needed,
searches the web for documentation and examples, and produces a research
brief that gets written to the workspace as RESEARCH.md.
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .claude_cli import ClaudeCLI
from .json_utils import extract_json
from .sanitize import sanitize_text

log = logging.getLogger(__name__)


EXTRACT_TOPICS_SYSTEM = """You are a research analyst preparing for a software development job.

Given a job description, identify ALL technologies, APIs, libraries, frameworks,
protocols, and services that you'd need to research before building.

Be thorough. If the job mentions "Stripe integration", you need the Stripe API docs.
If it mentions "MCP server", you need the Model Context Protocol spec.
If it mentions a specific npm package, you need its README.

Respond with ONLY valid JSON:
{
    "search_queries": [
        "specific search queries to find docs/examples (max 8)"
    ],
    "packages": {
        "npm": ["package names to look up on npmjs.com"],
        "pypi": ["package names to look up on pypi.org"]
    },
    "doc_urls": [
        "any specific documentation URLs mentioned or implied"
    ],
    "key_technologies": [
        "list of technologies/frameworks/APIs referenced"
    ]
}"""

SYNTHESIZE_SYSTEM = """You are a research analyst producing a technical brief for a developer.

Given raw research materials (search results, documentation excerpts, package info),
synthesize them into a clear, actionable research brief.

Focus on:
1. Key APIs and their endpoints/methods the developer will need
2. Package installation commands and basic usage patterns
3. Code examples and patterns relevant to the job
4. Common pitfalls or gotchas
5. Authentication/setup requirements

Be concise but thorough. Include actual code snippets and API signatures.
Output clean markdown."""


def _run_web_search(query: str, count: int = 5) -> list[dict]:
    """Run a web search using the system's search capability.

    Falls back gracefully if no search tool is available.
    """
    try:
        result = subprocess.run(
            ["node", "-e", f"""
const https = require('https');
const q = {json.dumps(query)};
const url = `https://api.search.brave.com/res/v1/web/search?q=${{encodeURIComponent(q)}}&count={count}`;
const key = process.env.BRAVE_API_KEY || '';
if (!key) {{ console.log(JSON.stringify([])); process.exit(0); }}
const req = https.request(url, {{headers: {{'X-Subscription-Token': key, 'Accept': 'application/json'}}}}, (res) => {{
    let data = '';
    res.on('data', c => data += c);
    res.on('end', () => {{
        try {{
            const j = JSON.parse(data);
            const results = (j.web?.results || []).map(r => ({{title: r.title, url: r.url, snippet: r.description}}));
            console.log(JSON.stringify(results));
        }} catch(e) {{ console.log(JSON.stringify([])); }}
    }});
}});
req.on('error', () => console.log(JSON.stringify([])));
req.end();
"""],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0 and result.stdout.strip():
            return json.loads(result.stdout.strip())
    except Exception as e:
        log.debug(f"Web search failed for '{query}': {e}")
    return []


def _fetch_url_text(url: str, max_chars: int = 8000) -> str:
    """Fetch a URL and extract readable text content."""
    try:
        result = subprocess.run(
            ["node", "-e", f"""
const https = require('https');
const http = require('http');
const url = {json.dumps(url)};
const client = url.startsWith('https') ? https : http;
const req = client.get(url, {{headers: {{'User-Agent': 'Mozilla/5.0'}}, timeout: 10000}}, (res) => {{
    if (res.statusCode >= 300 && res.statusCode < 400 && res.headers.location) {{
        console.log('REDIRECT:' + res.headers.location);
        return;
    }}
    let data = '';
    res.on('data', c => {{ data += c; if (data.length > {max_chars * 2}) res.destroy(); }});
    res.on('end', () => {{
        // Strip HTML tags for basic text extraction
        const text = data.replace(/<script[^>]*>[\\s\\S]*?<\\/script>/gi, '')
                        .replace(/<style[^>]*>[\\s\\S]*?<\\/style>/gi, '')
                        .replace(/<[^>]+>/g, ' ')
                        .replace(/\\s+/g, ' ')
                        .trim()
                        .substring(0, {max_chars});
        console.log(text);
    }});
}});
req.on('error', (e) => console.log('ERROR:' + e.message));
req.setTimeout(10000, () => {{ req.destroy(); console.log('ERROR:timeout'); }});
"""],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode == 0:
            text = result.stdout.strip()
            if not text.startswith("ERROR:"):
                return text[:max_chars]
    except Exception as e:
        log.debug(f"URL fetch failed for '{url}': {e}")
    return ""


def _lookup_npm_package(name: str) -> str:
    """Look up an npm package and return its description + README excerpt."""
    url = f"https://registry.npmjs.org/{name}"
    try:
        result = subprocess.run(
            ["node", "-e", f"""
const https = require('https');
https.get({json.dumps(url)}, (res) => {{
    let data = '';
    res.on('data', c => data += c);
    res.on('end', () => {{
        try {{
            const pkg = JSON.parse(data);
            const latest = pkg['dist-tags']?.latest || '';
            const ver = pkg.versions?.[latest] || {{}};
            const info = {{
                name: pkg.name,
                version: latest,
                description: pkg.description || '',
                homepage: ver.homepage || pkg.homepage || '',
                keywords: (ver.keywords || []).slice(0, 10),
                readme: (pkg.readme || '').substring(0, 3000),
            }};
            console.log(JSON.stringify(info));
        }} catch(e) {{ console.log(JSON.stringify({{error: 'parse'}})); }}
    }});
}}).on('error', () => console.log(JSON.stringify({{error: 'network'}})));
"""],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            if "error" not in data:
                parts = [f"**{data['name']}** v{data['version']}: {data['description']}"]
                if data.get("readme"):
                    parts.append(data["readme"][:2000])
                return "\n\n".join(parts)
    except Exception as e:
        log.debug(f"npm lookup failed for '{name}': {e}")
    return ""


def _lookup_pypi_package(name: str) -> str:
    """Look up a PyPI package and return its description."""
    url = f"https://pypi.org/pypi/{name}/json"
    try:
        result = subprocess.run(
            ["node", "-e", f"""
const https = require('https');
https.get({json.dumps(url)}, (res) => {{
    let data = '';
    res.on('data', c => data += c);
    res.on('end', () => {{
        try {{
            const pkg = JSON.parse(data);
            const info = {{
                name: pkg.info?.name,
                version: pkg.info?.version,
                summary: pkg.info?.summary || '',
                description: (pkg.info?.description || '').substring(0, 2000),
                home_page: pkg.info?.home_page || pkg.info?.project_url || '',
            }};
            console.log(JSON.stringify(info));
        }} catch(e) {{ console.log(JSON.stringify({{error: 'parse'}})); }}
    }});
}}).on('error', () => console.log(JSON.stringify({{error: 'network'}})));
"""],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            data = json.loads(result.stdout.strip())
            if "error" not in data:
                parts = [f"**{data['name']}** v{data['version']}: {data['summary']}"]
                if data.get("description"):
                    parts.append(data["description"][:1500])
                return "\n\n".join(parts)
    except Exception as e:
        log.debug(f"PyPI lookup failed for '{name}': {e}")
    return ""


@dataclass
class ResearchBrief:
    """Result of the research phase."""
    content: str
    sources: list[str] = field(default_factory=list)
    search_queries: list[str] = field(default_factory=list)
    packages_found: list[str] = field(default_factory=list)


class Researcher:
    """Conducts deep research before building starts."""

    def __init__(self, claude: ClaudeCLI):
        self.claude = claude

    def research_job(self, job_title: str, job_description: str) -> ResearchBrief:
        """Run deep research for a job and return a structured brief.

        Steps:
        1. Extract key technologies and search queries from job description
        2. Run web searches for documentation and examples (if BRAVE_API_KEY set)
        3. Look up specific packages on npm/pypi
        4. Fetch key documentation pages
        5. Synthesize everything into an actionable research brief
        """
        safe_title = sanitize_text(job_title, max_length=500)
        safe_desc = sanitize_text(job_description, max_length=8000)

        has_brave_key = bool(os.environ.get("BRAVE_API_KEY"))
        if not has_brave_key:
            log.warning(
                "BRAVE_API_KEY not set — web search disabled. "
                "Research will rely on package lookups and LLM knowledge only."
            )

        # Step 1: Extract research topics
        log.info("Research phase: extracting topics...")
        topics = self._extract_topics(safe_title, safe_desc)

        # Step 2: Run web searches (only if API key available)
        raw_materials: list[str] = []
        sources: list[str] = []

        if has_brave_key:
            for query in topics.get("search_queries", [])[:8]:
                log.info(f"Research: searching '{query}'")
                results = _run_web_search(query)
                for r in results[:3]:
                    raw_materials.append(
                        f"### Search: {query}\n"
                        f"**{r.get('title', '')}** — {r.get('url', '')}\n"
                        f"{r.get('snippet', '')}\n"
                    )
                    sources.append(r.get("url", ""))
        else:
            log.info("Research: skipping web search (no BRAVE_API_KEY)")

        # Step 3: Look up packages
        packages_found = []
        for pkg in topics.get("packages", {}).get("npm", [])[:5]:
            log.info(f"Research: looking up npm/{pkg}")
            info = _lookup_npm_package(pkg)
            if info:
                raw_materials.append(f"### npm: {pkg}\n{info}\n")
                packages_found.append(f"npm/{pkg}")

        for pkg in topics.get("packages", {}).get("pypi", [])[:5]:
            log.info(f"Research: looking up pypi/{pkg}")
            info = _lookup_pypi_package(pkg)
            if info:
                raw_materials.append(f"### pypi: {pkg}\n{info}\n")
                packages_found.append(f"pypi/{pkg}")

        # Step 4: Fetch specific doc URLs
        for url in topics.get("doc_urls", [])[:4]:
            log.info(f"Research: fetching {url}")
            text = _fetch_url_text(url, max_chars=4000)
            if text and len(text) > 100:
                raw_materials.append(f"### Documentation: {url}\n{text[:4000]}\n")
                sources.append(url)

        # Step 5: Synthesize
        if not raw_materials:
            log.info("Research: no materials found, producing brief from job description only")
            brief_content = self._synthesize_from_description(safe_title, safe_desc, topics)
        else:
            log.info(f"Research: synthesizing {len(raw_materials)} sources into brief")
            brief_content = self._synthesize(safe_title, safe_desc, raw_materials)

        return ResearchBrief(
            content=brief_content,
            sources=[s for s in sources if s],
            search_queries=topics.get("search_queries", []),
            packages_found=packages_found,
        )

    def _extract_topics(self, title: str, description: str) -> dict:
        """Use LLM to identify what needs researching."""
        user = f"Job Title: {title}\n\nJob Description:\n{description}"
        try:
            text = self.claude.create_message(
                system=EXTRACT_TOPICS_SYSTEM,
                user=user,
                max_tokens=1024,
            )
            result = extract_json(text, fallback=None)
            if result:
                return result
        except RuntimeError as e:
            log.warning(f"Topic extraction failed: {e}")

        # Fallback: extract obvious technology names
        return self._fallback_extract(title, description)

    def _fallback_extract(self, title: str, description: str) -> dict:
        """Regex-based fallback for topic extraction if LLM fails."""
        combined = f"{title} {description}".lower()
        queries = []

        tech_patterns = {
            "near protocol": "NEAR Protocol SDK documentation",
            "mcp": "Model Context Protocol MCP server tutorial",
            "langchain": "LangChain documentation getting started",
            "discord bot": "Discord.js bot tutorial 2025",
            "telegram bot": "Telegram Bot API python tutorial",
            "chrome extension": "Chrome Extension Manifest V3 tutorial",
            "stripe": "Stripe API integration guide",
            "openai": "OpenAI API documentation",
            "anthropic": "Anthropic Claude API documentation",
            "supabase": "Supabase documentation getting started",
            "firebase": "Firebase documentation setup",
            "graphql": "GraphQL API tutorial",
            "websocket": "WebSocket implementation guide",
            "docker": "Dockerfile best practices",
            "rust": "Rust programming tutorial",
            "solidity": "Solidity smart contract tutorial",
            "react": "React documentation",
            "next.js": "Next.js documentation",
            "fastapi": "FastAPI documentation tutorial",
            "express": "Express.js REST API tutorial",
        }

        for tech, query in tech_patterns.items():
            if tech in combined:
                queries.append(query)

        # Also search for the job title itself
        if title:
            queries.insert(0, f"{title} tutorial example")

        return {
            "search_queries": queries[:8],
            "packages": {"npm": [], "pypi": []},
            "doc_urls": [],
            "key_technologies": [],
        }

    def _synthesize(self, title: str, description: str, materials: list[str]) -> str:
        """Synthesize raw research materials into an actionable brief."""
        materials_text = "\n\n".join(materials)
        if len(materials_text) > 20000:
            materials_text = materials_text[:20000] + "\n\n... (truncated)"

        user = (
            f"# Job\n**{title}**\n\n{description}\n\n"
            f"# Research Materials\n\n{materials_text}\n\n"
            f"Synthesize these materials into a clear technical brief "
            f"that a developer can use to build this job's deliverable."
        )

        try:
            return self.claude.create_message(
                system=SYNTHESIZE_SYSTEM,
                user=user,
                max_tokens=4096,
            )
        except RuntimeError as e:
            log.warning(f"Synthesis failed: {e}")
            # Return raw materials as fallback
            return f"# Research Materials\n\n{materials_text}"

    def _synthesize_from_description(self, title: str, description: str, topics: dict) -> str:
        """Produce a brief from just the job description when no search results found."""
        techs = topics.get("key_technologies", [])
        tech_list = ", ".join(techs) if techs else "general"

        return (
            f"# Research Brief\n\n"
            f"**Job:** {title}\n"
            f"**Key Technologies:** {tech_list}\n\n"
            f"No external research materials were found. "
            f"Build using training knowledge and the NEAR reference guide.\n\n"
            f"## Key Requirements\n\n{description[:4000]}\n"
        )
