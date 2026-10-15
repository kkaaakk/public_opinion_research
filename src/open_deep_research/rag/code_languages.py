# 维护代码扩展名与 LangChain 语言枚举的映射，供语言感知分块使用。
"""Code file language mapping for RAG loading and splitting."""

from langchain_text_splitters import Language

CODE_EXTENSION_LANGUAGE_MAP: dict[str, str] = {
    ".bas": "visualbasic6",
    ".c": "c",
    ".cc": "cpp",
    ".cjs": "js",
    ".cob": "cobol",
    ".cobol": "cobol",
    ".cpp": "cpp",
    ".cs": "csharp",
    ".cxx": "cpp",
    ".eex": "elixir",
    ".ex": "elixir",
    ".exs": "elixir",
    ".go": "go",
    ".h": "c",
    ".hh": "cpp",
    ".hpp": "cpp",
    ".hs": "haskell",
    ".htm": "html",
    ".html": "html",
    ".hxx": "cpp",
    ".java": "java",
    ".js": "js",
    ".jsx": "js",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".lua": "lua",
    ".mjs": "js",
    ".p6": "perl",
    ".php": "php",
    ".pl": "perl",
    ".pm": "perl",
    ".proto": "proto",
    ".ps1": "powershell",
    ".psd1": "powershell",
    ".psm1": "powershell",
    ".py": "python",
    ".pyi": "python",
    ".r": "r",
    ".rb": "ruby",
    ".rs": "rust",
    ".scala": "scala",
    ".sc": "scala",
    ".sol": "sol",
    ".swift": "swift",
    ".ts": "ts",
    ".tsx": "ts",
    ".vb": "visualbasic6",
}

LANGUAGE_ALIASES: dict[str, str] = {
    "c#": "csharp",
    "c++": "cpp",
    "csharp": "csharp",
    "javascript": "js",
    "perl6": "perl",
    "powershell": "powershell",
    "python": "python",
    "typescript": "ts",
}

SUPPORTED_LANGUAGE_VALUES = {language.value for language in Language}


# 按源代码扩展名查询对应的分块语言。
def language_for_extension(extension: str) -> str | None:
    """Return a LangChain language value for a source code extension."""
    normalized_extension = extension.strip().lower()
    if normalized_extension and not normalized_extension.startswith("."):
        normalized_extension = f".{normalized_extension}"
    language = CODE_EXTENSION_LANGUAGE_MAP.get(normalized_extension)
    if language in SUPPORTED_LANGUAGE_VALUES:
        return language
    return None


# 归一化语言名称并解析为 LangChain 的 Language 枚举。
def language_enum_from_name(language: str | None) -> Language | None:
    """Normalize a language name and return LangChain's Language enum."""
    normalized_language = str(language or "").strip().lower()
    normalized_language = LANGUAGE_ALIASES.get(normalized_language, normalized_language)
    if normalized_language not in SUPPORTED_LANGUAGE_VALUES:
        return None
    return Language(normalized_language)
