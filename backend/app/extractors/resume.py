"""Deterministic resume intake for job discovery (Stream E, R4-10).

Before this module, an uploaded resume reached the planner as a
filename only (``agentLoop.py:297-308``) — its content was never read.
These helpers turn an uploaded resume (TXT / PDF / DOCX) into plain
text plus a parsed profile (skills, experience, education, target
roles, location) with **no LLM and no network**: skill matching is a
curated vocabulary lookup, everything else is regex over the text.

Nothing here submits an application, contacts a recruiter, or stores
a real credential. Profiles are built from the resume the operator
uploaded in this session.
"""
from __future__ import annotations

import io
import re
from dataclasses import dataclass, field

# Curated skill vocabulary, grouped so a resume line like
# "Languages: Python, Java" and a listing tag "python" meet on the
# same canonical token. Matching is case-insensitive word-boundary
# search; aliases map common variants to the canonical skill.
SKILL_VOCABULARY: dict[str, tuple[str, ...]] = {
    "Python": ("python", "py"),
    "Java": ("java",),
    "JavaScript": ("javascript", "js"),
    "TypeScript": ("typescript", "ts"),
    "C": ("c language",),
    "C++": ("c++", "cpp"),
    "C#": ("c#", "csharp"),
    "Go": ("golang", "go language"),
    "Rust": ("rust",),
    "Ruby": ("ruby",),
    "PHP": ("php",),
    "SQL": ("sql", "mysql", "postgres", "postgresql", "sqlite"),
    "React": ("react", "reactjs"),
    "Angular": ("angular",),
    "Vue": ("vue", "vuejs"),
    "Node.js": ("node.js", "nodejs", "node"),
    "FastAPI": ("fastapi",),
    "Django": ("django",),
    "Flask": ("flask",),
    "Spring Boot": ("spring boot", "springboot", "spring"),
    "Express": ("express", "express.js"),
    "HTML": ("html", "html5"),
    "CSS": ("css", "css3"),
    "Tailwind": ("tailwind",),
    "Docker": ("docker",),
    "Kubernetes": ("kubernetes", "k8s"),
    "AWS": ("aws", "amazon web services"),
    "Azure": ("azure",),
    "GCP": ("gcp", "google cloud"),
    "Git": ("git", "github"),
    "Linux": ("linux",),
    "Machine Learning": ("machine learning", "ml"),
    "Deep Learning": ("deep learning",),
    "NLP": ("nlp", "natural language processing"),
    "Computer Vision": ("computer vision",),
    "TensorFlow": ("tensorflow",),
    "PyTorch": ("pytorch",),
    "Pandas": ("pandas",),
    "NumPy": ("numpy",),
    "Data Analysis": ("data analysis",),
    "Power BI": ("power bi",),
    "Excel": ("excel", "ms excel"),
    "Figma": ("figma",),
    "REST API": ("rest api", "restful", "rest"),
    "GraphQL": ("graphql",),
    "MongoDB": ("mongodb", "mongo"),
    "Redis": ("redis",),
    "Kafka": ("kafka",),
    "CI/CD": ("ci/cd", "cicd"),
    "Testing": ("pytest", "unit testing", "selenium"),
    "Agile": ("agile", "scrum"),
    "Communication": ("communication",),
    "Problem Solving": ("problem solving",),
}

# Role keywords used to infer what the candidate is looking for, and
# to match listing titles. Checked against the resume text and, for
# ranking, against each listing title.
ROLE_KEYWORDS: tuple[str, ...] = (
    "software engineer", "software developer", "backend developer",
    "backend engineer", "frontend developer", "frontend engineer",
    "full stack developer", "full-stack developer", "fullstack developer",
    "data analyst", "data scientist", "machine learning engineer",
    "ml engineer", "ai engineer", "devops engineer", "sde", "sde intern",
    "web developer", "python developer", "java developer",
    "react developer", "intern", "fresher", "graduate engineer trainee",
)

DEGREE_RE = re.compile(
    r"\b(b\.?tech|b\.?e\.?|bachelor(?:'s)?(?: of)? (?:technology|engineering|science|computer)|"
    r"m\.?tech|m\.?sc|master(?:'s)?(?: of)? (?:technology|science|computer)|"
    r"bca|mca|b\.?sc|diploma)\b",
    re.IGNORECASE,
)
_YEARS_RE = re.compile(r"(\d+(?:\.\d+)?)\s*\+?\s*(?:years?|yrs?)\b", re.IGNORECASE)
_EMAIL_RE = re.compile(r"[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+")
_PHONE_RE = re.compile(r"\+?\d[\d\s\-()]{8,14}\d")

KNOWN_CITIES = (
    "hyderabad", "bangalore", "bengaluru", "mumbai", "delhi", "new delhi",
    "chennai", "pune", "kolkata", "ahmedabad", "gurgaon", "gurugram",
    "noida", "jaipur", "kochi", "coimbatore", "remote",
)


@dataclass
class ResumeProfile:
    """Parsed resume. `raw_text` is kept for audit; matching uses skills/roles."""

    name: str | None = None
    email: str | None = None
    phone: str | None = None
    skills: list[str] = field(default_factory=list)
    target_roles: list[str] = field(default_factory=list)
    years_experience: float | None = None
    education: list[str] = field(default_factory=list)
    locations: list[str] = field(default_factory=list)
    raw_text: str = ""

    def summary_line(self) -> str:
        bits = []
        if self.target_roles:
            bits.append("targeting " + ", ".join(self.target_roles[:3]))
        if self.skills:
            bits.append("skills: " + ", ".join(self.skills[:8]))
        if self.years_experience is not None:
            bits.append(f"{self.years_experience:g} years experience")
        if self.locations:
            bits.append("prefers " + ", ".join(self.locations[:3]))
        return "; ".join(bits) or "no parsable profile"


def extract_text(file_bytes: bytes, filename: str) -> str:
    """Extract plain text from an uploaded resume file.

    TXT is decoded as UTF-8; PDF via pypdf (page text); DOCX via
    python-docx (paragraphs + tables). Raises ValueError on an
    unsupported type or an unreadable file — never returns a
    filename-only placeholder.
    """
    name = (filename or "").lower()
    if name.endswith(".txt") or name.endswith(".csv"):
        return file_bytes.decode("utf-8", errors="replace")
    if name.endswith(".pdf"):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(file_bytes))
        parts = []
        for page in reader.pages:
            try:
                parts.append(page.extract_text() or "")
            except Exception:
                continue
        text = "\n".join(parts)
        if not text.strip():
            raise ValueError("PDF contains no extractable text (scanned image?)")
        return text
    if name.endswith(".docx"):
        from docx import Document

        doc = Document(io.BytesIO(file_bytes))
        parts = [p.text for p in doc.paragraphs]
        for table in doc.tables:
            for row in table.rows:
                parts.extend(cell.text for cell in row.cells)
        text = "\n".join(parts)
        if not text.strip():
            raise ValueError("DOCX contains no extractable text")
        return text
    raise ValueError(f"unsupported resume type: {filename!r} (use .txt, .pdf or .docx)")


def parse_resume(text: str) -> ResumeProfile:
    """Deterministically parse resume text into a ResumeProfile."""
    clean = (text or "").strip()
    lowered = clean.lower()
    profile = ResumeProfile(raw_text=clean)

    # Name heuristic: first non-empty line that looks like a person name
    # (2-4 title-case words, no digits, no @, not a section header).
    for line in clean.splitlines():
        line = line.strip()
        if not line or len(line) > 60 or "@" in line or any(ch.isdigit() for ch in line):
            continue
        words = line.split()
        if 2 <= len(words) <= 4 and all(w[0].isupper() for w in words if w[:1].isalpha()):
            if line.lower() not in {"curriculum vitae", "resume", "cv"}:
                profile.name = line
                break

    email = _EMAIL_RE.search(clean)
    if email:
        profile.email = email.group(0)
    phone = _PHONE_RE.search(clean)
    if phone:
        profile.phone = phone.group(0).strip()

    # Skills: canonical skill iff any alias matches on a word boundary.
    for canonical, aliases in SKILL_VOCABULARY.items():
        for alias in aliases:
            if re.search(r"(?<![A-Za-z0-9+#.])" + re.escape(alias) + r"(?![A-Za-z0-9+#])", lowered):
                profile.skills.append(canonical)
                break

    for role in ROLE_KEYWORDS:
        if role in lowered:
            profile.target_roles.append(role)

    years = _YEARS_RE.findall(clean)
    if years:
        try:
            profile.years_experience = max(float(y) for y in years)
        except ValueError:
            pass

    for match in DEGREE_RE.finditer(clean):
        degree = match.group(0).strip()
        if degree not in profile.education:
            profile.education.append(degree)

    for city in KNOWN_CITIES:
        if re.search(r"\b" + re.escape(city) + r"\b", lowered):
            profile.locations.append(city.title() if city != "remote" else "Remote")

    return profile
