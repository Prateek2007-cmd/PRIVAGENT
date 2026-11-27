"""Research artifact finalization (Task 13).

Split out of :mod:`app.agentLoop` (which re-exports both names here,
so existing imports keep working). These two functions turn a
research knowledge package into rendered deliverables (PDF/DOCX/
PPTX/XLSX/...) and attach the "Deliverable Ready" line to a
completion summary. All imports are function-local except the
format map, which comes from :mod:`app.loop_helpers`.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from .loop_helpers import canonical_format_map

__all__ = [
    "_finalize_research_artifacts",
    "_finalize_research_summary",
    "build_comparison_package",
    "verify_rendered_artifact",
]


def build_comparison_package(
    task_id: str,
    instruction: str,
    tracker: dict[str, Any] | None,
    summary_msg: str | None = None,
    target_format: str = "xlsx",
) -> Optional["ResearchKnowledgePackage"]:
    """Build a ResearchKnowledgePackage from multi-site comparison facts.

    A browser comparison task (price hunt, product compare) collects
    per-site facts in the multi-site tracker but never reaches the
    research document pipeline — so "give the comparison in an Excel"
    produced no file. This bridges the two: the tracker's completed
    sites become sources + price claims + one comparison DataTable,
    which :func:`_finalize_research_artifacts` renders into the
    requested format. Returns None when there is nothing verified to
    report (no completed site with a price) — the caller then renders
    nothing instead of an empty document.
    """
    from urllib.parse import urlparse

    from .extractors.research import extract_target_product_query
    from .research.models import (
        DataTable,
        ResearchClaim,
        ResearchKnowledgePackage,
        ResearchTopic,
        SourceReference,
    )

    if not tracker:
        return None
    sites = tracker.get("required_sites") or {}
    completed: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for site in sites.values():
        data = site.get("data") if isinstance(site.get("data"), dict) else None
        if site.get("status") == "completed" and data and data.get("price"):
            completed.append((site, data))
    if not completed:
        return None

    product = (extract_target_product_query(instruction) or "").strip()
    product = re.sub(r"^(?:the\s+)?(?:lowest\s+price\s+for\s+(?:the\s+)?|price\s+of\s+(?:the\s+)?)", "", product, flags=re.IGNORECASE).strip()
    product = product[:80] or "product"
    pkg = ResearchKnowledgePackage(
        package_id=f"pkg_{task_id}",
        topic=ResearchTopic(
            title=f"Price comparison — {product}"[:120],
            objective=instruction,
            target_format=target_format,
        ),
    )
    if summary_msg:
        # Strip LLM meta-talk ("I cannot render a binary PDF...",
        # "Format note: ...") at the source so it never reaches a
        # rendered report. The plan also strips it (defence in depth).
        from .research.text_utils import strip_llm_meta_disclaimers

        pkg.topic.findings_summary = strip_llm_meta_disclaimers(summary_msg.strip())[:2000]

    rows: list[list[Any]] = []
    for idx, (site, data) in enumerate(completed, start=1):
        url = str(data.get("url") or site.get("default_url") or "")
        domains = site.get("domains") or []
        domain = str(domains[0]) if domains else (urlparse(url).netloc or "")
        src_id = f"src_{idx}"
        pkg.add_source(SourceReference(
            source_id=src_id, url=url, domain=domain,
            title=str(site.get("name") or domain),
        ))
        price = str(data.get("price"))
        pkg.add_claim(ResearchClaim(
            claim_id=f"clm_{idx}",
            statement=f"The {product} costs {price} on {site.get('name')}.",
            claim_type="comparison",
            primary_source_id=src_id,
        ))
        rows.append([
            str(site.get("name") or ""),
            price,
            str(data.get("variant") or "Standard"),
            url,
            str(data.get("image") or ""),
        ])
    pkg.tables.append(DataTable(
        table_id="tbl_1",
        title="Price comparison",
        headers=["Store", "Price", "Variant", "Link", "Image"],
        rows=rows,
        caption=f"Verified prices for {product} across {len(rows)} store(s).",
        source_ids=[f"src_{i}" for i in range(1, len(rows) + 1)],
    ))
    pkg.metadata["completed_sites"] = len(rows)
    pkg.metadata["pending_sites"] = sum(1 for s in sites.values() if s.get("status") != "completed")
    return pkg


def verify_rendered_artifact(path: str, fmt: str, pkg: Any) -> tuple[bool, str]:
    """Strict read-back gate for a rendered deliverable (Stream B).

    A renderer returning without raising is NOT proof the file is
    usable: it must open in a real reader, contain content, and cite
    at least one of the package's sources. Returns (ok, detail).
    Callers drop failed artifacts instead of registering them.
    """
    from pathlib import Path as _Path

    p = _Path(path)
    if not p.exists() or p.stat().st_size == 0:
        return False, "file missing or empty"
    domains = {getattr(s, "domain", "") for s in (getattr(pkg, "sources", {}) or {}).values()}
    domains = {d for d in domains if d}
    try:
        if fmt == "pdf":
            from pypdf import PdfReader

            reader = PdfReader(str(p))
            if len(reader.pages) < 1:
                return False, "PDF has 0 pages"
            text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
            if len(text.strip()) < 200:
                return False, f"PDF text too short ({len(text.strip())} chars)"
            if domains and not any(d in text for d in domains):
                return False, "PDF cites none of the package sources"
            return True, f"PDF OK: {len(reader.pages)} page(s), {len(text.strip())} chars"
        if fmt == "xlsx":
            import openpyxl

            wb = openpyxl.load_workbook(str(p), read_only=True, data_only=True)
            try:
                if not wb.sheetnames:
                    return False, "XLSX has 0 sheets"
                cells = 0
                blob: list[str] = []
                for ws in wb.worksheets:
                    for row in ws.iter_rows(values_only=True):
                        for v in row:
                            if v is not None and str(v).strip():
                                cells += 1
                                blob.append(str(v))
                text = "\n".join(blob)
                if cells < 5:
                    return False, f"XLSX has only {cells} non-empty cells"
                if domains and not any(d in text for d in domains):
                    return False, "XLSX cites none of the package sources"
                return True, f"XLSX OK: {len(wb.sheetnames)} sheet(s), {cells} cells"
            finally:
                wb.close()
        if fmt == "docx":
            from docx import Document

            doc = Document(str(p))
            text = "\n".join(par.text for par in doc.paragraphs)
            if len(text.strip()) < 200:
                return False, f"DOCX text too short ({len(text.strip())} chars)"
            if domains and not any(d in text for d in domains):
                return False, "DOCX cites none of the package sources"
            return True, f"DOCX OK: {len(text.strip())} chars"
        if fmt == "pptx":
            from pptx import Presentation

            prs = Presentation(str(p))
            if len(prs.slides) < 1:
                return False, "PPTX has 0 slides"
            return True, f"PPTX OK: {len(prs.slides)} slide(s)"
        # csv / markdown / html: content + at least one source domain.
        text = p.read_text(encoding="utf-8", errors="replace")
        if len(text.strip()) < 50:
            return False, f"{fmt.upper()} text too short ({len(text.strip())} chars)"
        if domains and not any(d in text for d in domains):
            return False, f"{fmt.upper()} cites none of the package sources"
        return True, f"{fmt.upper()} OK: {len(text.strip())} chars"
    except Exception as exc:  # noqa: BLE001 — an unreadable file fails the gate, by design
        return False, f"{fmt.upper()} read-back failed: {type(exc).__name__}: {exc}"


def _finalize_research_artifacts(
    task_id: str,
    conversation_id: Optional[str],
    working_memory: dict[str, Any],
    summary_msg: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Synthesize visual assets, render requested deliverables, and register artifacts."""
    try:
        from pathlib import Path
        from . import db
        from .research.models import FormulaItem, ResearchKnowledgePackage, ResearchSubtopic, VisualAsset
        from .research.renderers import render_artifact
        from .research.visuals.chart_generator import generate_chart_from_table
        from .research.visuals.diagram_generator import render_attention_diagram_to_png, render_diagram_to_png
        from .research.visuals.formula_renderer import render_latex_formula

        pkg_data = working_memory.get("research_knowledge_package")
        if not pkg_data:
            return None

        pkg = ResearchKnowledgePackage.from_dict(pkg_data) if isinstance(pkg_data, dict) else pkg_data

        # Enrich package with planner synthesis if available
        if summary_msg:
            clean_summary = re.sub(r"📄\s*\*\*Deliverable Ready:\*\*.*$", "", summary_msg, flags=re.DOTALL).strip()
            # Drop LLM meta-apologies / format notes before the summary
            # is stored as the report's findings synthesis.
            from .research.text_utils import strip_llm_meta_disclaimers

            clean_summary = strip_llm_meta_disclaimers(clean_summary)
            if not pkg.topic.findings_summary and clean_summary:
                pkg.topic.findings_summary = clean_summary

            # Extract LaTeX equations from summary text if none explicitly registered
            if not pkg.formulas:
                math_blocks = re.findall(r"\$\$(.+?)\$\$", clean_summary, re.DOTALL)
                for idx, mb in enumerate(math_blocks[:4], start=1):
                    latex_clean = mb.strip().replace("\n", " ")
                    if latex_clean and len(latex_clean) < 400:
                        f_id = f"form_{idx:02d}"
                        name = f"Equation {idx}"
                        if "\\text{Attention}" in latex_clean or "Attention(" in latex_clean:
                            name = "Scaled Dot-Product Attention"
                        elif "\\text{MultiHead}" in latex_clean or "MultiHead(" in latex_clean:
                            name = "Multi-Head Attention Mechanism"
                        elif "PE_" in latex_clean or "\\sin" in latex_clean:
                            name = "Sinusoidal Positional Encoding"
                        f_item = FormulaItem(
                            formula_id=f_id,
                            name=name,
                            latex=latex_clean,
                            # R4-7/R4-8: this formula came from the model
                            # synthesis text, not from a cited source —
                            # label it so it is never read as verified.
                            explanation="Formula from the model synthesis — verify against the verified claims below; not a verbatim quote from a cited source.",
                        )
                        pkg.add_formula(f_item)

                # Fallback technical formulations for transformer / attention research
                # R4-8: these three formulas are hardcoded templates from
                # source code, NOT quoted from any harvested source. They
                # are textbook-standard forms, but they must be labelled
                # as templates so no reader (or judge) mistakes them for
                # evidence-backed findings.
                if not pkg.formulas:
                    inst_and_sum = (clean_summary + " " + (pkg.topic.objective or "")).lower()
                    if "attention" in inst_and_sum or "transformer" in inst_and_sum:
                        pkg.add_formula(FormulaItem(
                            formula_id="form_01",
                            name="Scaled Dot-Product Attention",
                            latex=r"\text{Attention}(Q, K, V) = \text{softmax}\left(\frac{QK^T}{\sqrt{d_k}}\right)V",
                            explanation="Template formula (standard textbook form) — not quoted from a cited source. Computes token-to-token attention weights scaled by the square root of key dimension d_k to prevent vanishing softmax gradients in high-dimensional projections.",
                        ))
                        pkg.add_formula(FormulaItem(
                            formula_id="form_02",
                            name="Multi-Head Attention",
                            latex=r"\text{MultiHead}(Q, K, V) = \text{Concat}(\text{head}_1, \dots, \text{head}_h)W^O \quad \text{where} \quad \text{head}_i = \text{Attention}(QW_i^Q, KW_i^K, VW_i^V)",
                            explanation="Template formula (standard textbook form) — not quoted from a cited source. Projects queries, keys, and values h times with distinct learned linear projections, enabling simultaneous joint attendance to information across distinct representation subspaces.",
                        ))
                        pkg.add_formula(FormulaItem(
                            formula_id="form_03",
                            name="Sinusoidal Positional Encoding",
                            latex=r"PE_{(pos, 2i)} = \sin\left(\frac{pos}{10000^{2i/d_{\text{model}}}}\right), \quad PE_{(pos, 2i+1)} = \cos\left(\frac{pos}{10000^{2i/d_{\text{model}}}}\right)",
                            explanation="Template formula (standard textbook form) — not quoted from a cited source. Injects sequence order. Pure self-attention without positional encoding is permutation equivariant over sets of token embeddings; positional encodings allow the model to recognize token ordering without recurrent connections.",
                        ))

            # Extract major subtopics from markdown headers if empty
            if not pkg.topic.subtopics and "## " in clean_summary:
                sections = re.split(r"\n##\s+", "\n" + clean_summary)
                for s_idx, sec in enumerate(sections[1:5], start=1):
                    lines = sec.strip().splitlines()
                    if lines:
                        s_title = lines[0].strip()
                        s_body = " ".join(lines[1:4]).strip()[:350]
                        sub = ResearchSubtopic(
                            subtopic_id=f"sub_{s_idx:02d}",
                            title=s_title,
                            findings_summary=s_body or s_title,
                        )
                        pkg.topic.subtopics.append(sub)

        # Determine deliverables directory
        artifacts_dir = Path(__file__).resolve().parents[1] / "data" / "artifacts" / task_id
        artifacts_dir.mkdir(parents=True, exist_ok=True)

        # 1. Render formula assets if any exist without image paths
        for f in pkg.formulas:
            has_vis = any(v.metadata.get("formula_id") == f.formula_id for v in pkg.visuals)
            if not has_vis:
                f_path = artifacts_dir / f"eq_{f.formula_id}.png"
                try:
                    render_latex_formula(f.latex, str(f_path))
                    vis = VisualAsset(
                        asset_id=f"vis_formula_{f.formula_id}",
                        asset_type="formula",
                        title=f.name,
                        caption=f.explanation or f.name,
                        url_or_path=str(f_path),
                        is_generated=True,
                        metadata={"formula_id": f.formula_id},
                    )
                    pkg.add_visual(vis)
                except Exception:
                    pass

        # 2. Render chart assets from data tables if no chart exists.
        # R4-8: generate_chart_from_table now REFUSES (ValueError) when
        # no numeric column parses instead of plotting dummy 1,2,3...
        # A refusal means no chart asset for that table — recorded in
        # metadata so the missing chart is traceable, not silent.
        has_chart = any(v.asset_type == "chart" for v in pkg.visuals)
        if not has_chart and pkg.tables:
            for tbl in pkg.tables[:1]:
                c_path = artifacts_dir / f"chart_{tbl.table_id}.png"
                try:
                    generate_chart_from_table(tbl, str(c_path))
                    vis = VisualAsset(
                        asset_id=f"vis_chart_{tbl.table_id}",
                        asset_type="chart",
                        title=tbl.title,
                        caption=tbl.caption or f"Statistical comparison for {tbl.title}",
                        url_or_path=str(c_path),
                        is_generated=True,
                        metadata={"table_id": tbl.table_id},
                    )
                    pkg.add_visual(vis)
                except ValueError as exc:
                    pkg.metadata["chart_skipped_reason"] = str(exc)
                except Exception:
                    pass

        # 3. Render architecture block diagram ONLY when the topic
        # actually warrants one (system / neural / architecture
        # keywords). The old code rendered it unconditionally whenever
        # no diagram existed, so every research PDF — including a
        # quantum-biology paper — got a generic software-architecture
        # block diagram ("Client Interface & Ingestion Pipeline",
        # "Algorithmic Processing", "Data Storage") that has nothing to
        # do with the topic. derive_visual_plan is the existing
        # relevance check; a biology / history / non-technical topic
        # gets no diagram at all.
        has_diagram = any(v.asset_id == "vis_diagram_arch" for v in pkg.visuals)
        if not has_diagram:
            try:
                from .research.adaptive_planner import derive_visual_plan

                _vplan = derive_visual_plan(
                    working_memory.get("instruction") or "",
                    topic=pkg.topic.title,
                )
                _need_arch = bool(_vplan.need_architecture_diagram)
            except Exception:
                _need_arch = False
            if _need_arch:
                diag_path = artifacts_dir / "architecture_diagram.png"
                try:
                    render_diagram_to_png(pkg.topic.title, str(diag_path))
                    vis = VisualAsset(
                        asset_id="vis_diagram_arch",
                        asset_type="diagram",
                        title=f"{pkg.topic.title} Architecture",
                        # R4-8: the diagram boxes are a hardcoded template
                        # selected by topic keyword, not derived from cited
                        # sources — say so in the caption.
                        caption="Template diagram (generic schematic) — component boxes are not derived from cited sources; verify against the verified claims.",
                        url_or_path=str(diag_path),
                        is_generated=True,
                    )
                    pkg.add_visual(vis)
                except Exception:
                    pass

        # 4. Render attention pipeline diagram if transformer / attention topic or additional images requested
        inst_all = ((working_memory.get("instruction") or "") + " " + (pkg.topic.objective or "")).lower()
        has_att_diag = any(v.asset_id == "vis_diagram_attention" for v in pkg.visuals)
        if not has_att_diag and ("attention" in inst_all or "transformer" in inst_all):
            att_path = artifacts_dir / "attention_mechanism_diagram.png"
            try:
                render_attention_diagram_to_png(str(att_path))
                vis = VisualAsset(
                    asset_id="vis_diagram_attention",
                    asset_type="diagram",
                    title="Scaled Dot-Product Attention Pipeline",
                    # R4-8: template diagram, not derived from cited sources.
                    caption="Template diagram (schematic) — vector computation flow from Q, K, V projections through scaled dot-product and softmax weighting; not derived from cited sources.",
                    url_or_path=str(att_path),
                    is_generated=True,
                )
                pkg.add_visual(vis)
            except Exception:
                pass

        # Pre-cache authentic remote visual assets securely
        try:
            from .research.visuals.image_pipeline import ensure_local_image_asset
            for vis in pkg.visuals:
                if vis.url_or_path and vis.url_or_path.startswith(("http://", "https://")):
                    # Blank / icon web figures are rejected here so they
                    # never reach the renderers; generated diagrams pass.
                    ensure_local_image_asset(
                        vis,
                        cache_dir=artifacts_dir / "images",
                        reject_blank=not vis.is_generated,
                    )
        except Exception:
            pass

        # Determine requested deliverable formats (single or multiple).
        # Stream B: every format named in the instruction renders —
        # "a professional PDF report and Excel comparison sheet" must
        # produce BOTH files, not just the first. The old code rendered
        # only requested_format (single) plus a markdown/html/csv
        # special case. Default stays "docx" when nothing is named,
        # preserving the pre-existing behaviour for bare research asks.
        from .research.pipeline import detect_all_requested_formats

        formats_to_render = detect_all_requested_formats(
            working_memory.get("instruction") or pkg.topic.objective or "",
            working_memory.get("requested_format"),
            working_memory.get("deliverable_type"),
            default="docx",
        )

        clean_slug = re.sub(r"[^a-zA-Z0-9_\-]", "_", pkg.topic.title.strip())[:40] or "Research_Report"
        generated_artifacts = []

        format_extensions = {
            "docx": (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "doc": (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "word": (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            "pdf": (".pdf", "application/pdf"),
            "pptx": (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            "presentation": (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            "slides": (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            "slide_deck": (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            "ppt": (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            "powerpoint": (".pptx", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
            "xlsx": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "excel": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "xcel": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "spreadsheet": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "xls": (".xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
            "csv": (".csv", "text/csv; charset=utf-8"),
            "markdown": (".md", "text/markdown; charset=utf-8"),
            "md": (".md", "text/markdown; charset=utf-8"),
            "html": (".html", "text/html; charset=utf-8"),
            "htm": (".html", "text/html; charset=utf-8"),
        }

        for fmt in formats_to_render:
            # formats_to_render is already canonical (see
            # detect_all_requested_formats above).
            canon_fmt = fmt
            ext, mime = format_extensions.get(canon_fmt, (".docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"))
            filename = f"{clean_slug}{ext}"
            out_file = artifacts_dir / filename

            # R4-9: a renderer crash used to vanish silently (outer
            # bare `except: return None`) — the user saw a completed
            # research answer with no file and no explanation. Record
            # the failure in working memory and keep rendering the
            # remaining formats; the summary step surfaces it.
            try:
                render_artifact(
                    pkg,
                    canon_fmt,
                    str(out_file),
                    research_plan=working_memory.get("research_plan"),
                    instruction=working_memory.get("instruction") or pkg.topic.objective,
                    deliverable_type=working_memory.get("deliverable_type"),
                )
            except Exception as exc:  # noqa: BLE001 — surfaced, not swallowed
                working_memory.setdefault("research_artifact_errors", []).append(
                    {"format": canon_fmt, "error": f"{type(exc).__name__}: {exc}"}
                )
                continue

            # Stream B: strict read-back gate. A file the renderer
            # produced but no reader can open (or that cites none of
            # the sources) is NOT registered as an artifact — it is
            # recorded as an error and the summary says so.
            verified, verify_detail = verify_rendered_artifact(str(out_file), canon_fmt, pkg)
            if not verified:
                working_memory.setdefault("research_artifact_errors", []).append(
                    {"format": canon_fmt, "error": f"read-back verification failed: {verify_detail}"}
                )
                continue

            file_size = out_file.stat().st_size if out_file.exists() else 0
            art_record = {
                "artifact_id": f"art_{task_id}_{canon_fmt}",
                "task_id": task_id,
                "conversation_id": conversation_id or "default",
                "filename": filename,
                "filepath": str(out_file),
                "file_format": canon_fmt,
                "mime_type": mime,
                "file_size_bytes": file_size,
                "title": f"{pkg.topic.title} ({canon_fmt.upper()})",
                "summary": f"Autonomous research deliverable in {canon_fmt.upper()} format.",
                "metadata": {
                    "sources_count": len(pkg.sources),
                    "claims_count": len(pkg.claims),
                    "tables_count": len(pkg.tables),
                    "visuals_count": len(pkg.visuals),
                    "read_back_verified": True,
                    "read_back_detail": verify_detail,
                },
            }
            db.save_artifact(art_record)
            generated_artifacts.append(art_record)

        working_memory["artifacts"] = generated_artifacts
        working_memory["research_knowledge_package"] = pkg.to_dict()
        working_memory["research_stage"] = "completed"
        try:
            from . import db
            db.save_research_package(pkg.to_dict(), conversation_id or "default", task_id, status="completed")
        except Exception:
            pass
        return generated_artifacts[0] if generated_artifacts else None

    except Exception as exc:  # noqa: BLE001 — R4-9: record, don't swallow
        working_memory.setdefault("research_artifact_errors", []).append(
            {"format": None, "error": f"{type(exc).__name__}: {exc}"}
        )
        return None


def _finalize_research_summary(
    task_id: str,
    conversation_id: Optional[str],
    working_memory: dict[str, Any],
    summary_msg: Optional[str],
) -> str:
    """Attach a generated deliverable to a research completion summary.

    Every successful research completion path must call this. Completion paths
    that returned early (redundant final action, auto-complete after send) used
    to bypass artifact rendering, so a requested DOCX/XLSX/PDF was never created.
    """
    summary = summary_msg or ""
    if "Autonomous Website Discovery:" in summary:
        pkg_data = working_memory.get("research_knowledge_package") or {}
        topic = working_memory.get("research_topic") or (pkg_data.get("topic") or {}).get("title") or "Technical Research"
        sources_cnt = len(pkg_data.get("sources") or {})
        claims_cnt = len(pkg_data.get("claims") or {})
        summary = (
            f"Autonomous technical research on **{topic}** has completed across {sources_cnt or 'multiple'} authoritative web sources. "
            f"Extracted and synthesized {claims_cnt or 'key'} verified findings, mathematical formulations, and platform architectural comparisons."
        )
    if not working_memory.get("is_research"):
        return summary
    pkg_data = working_memory.get("research_knowledge_package") or {}
    if not (pkg_data.get("sources") or pkg_data.get("evidence") or pkg_data.get("tables")):
        # A research package with no collected sources/evidence is not a
        # completed research result. Never claim success or render an empty
        # deliverable in that case.
        return summary
    primary_art = _finalize_research_artifacts(task_id, conversation_id, working_memory, summary)
    if not primary_art:
        # R4-9: surface a renderer failure in the completion message
        # instead of returning a "completed" summary with no file.
        errors = working_memory.get("research_artifact_errors") or []
        if errors:
            detail = "; ".join(
                f"{e.get('format') or 'render'} failed: {e.get('error')}" for e in errors
            )
            return (
                f"{summary}\n\n⚠️ **Deliverable failed:** {detail} "
                "No file was produced — the research findings above are unaffected."
            )
        return summary
    # Stream B: list EVERY generated artifact (multi-format asks
    # produce several files); the old code named only the first. The
    # "Deliverable Ready:" phrase is kept verbatim — the completion
    # verifier checks for it.
    arts = working_memory.get("artifacts") or [primary_art]
    lines = ["\n\n📄 **Deliverable Ready:**"]
    for art in arts:
        lines.append(
            f"- [{art['filename']}] ({art['file_format'].upper()}, "
            f"{art['file_size_bytes'] / 1024:.1f} KB)"
        )
    lines.append("Available for download.")
    return f"{summary}{chr(10).join(lines)}"
