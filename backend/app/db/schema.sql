PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    seed_doi TEXT NOT NULL,
    settings_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'created',
    paper_count INTEGER NOT NULL DEFAULT 0,
    edge_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS papers (
    paper_key TEXT PRIMARY KEY,
    doi TEXT,
    openalex_id TEXT,
    semantic_scholar_id TEXT,
    normalized_title TEXT,
    title TEXT NOT NULL,
    abstract TEXT,
    year INTEGER,
    venue TEXT,
    url TEXT,
    pdf_url TEXT,
    reference_count INTEGER,
    citation_count INTEGER,
    raw_json TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_papers_doi
ON papers(doi)
WHERE doi IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_papers_openalex_id ON papers(openalex_id);
CREATE INDEX IF NOT EXISTS idx_papers_s2_id ON papers(semantic_scholar_id);
CREATE INDEX IF NOT EXISTS idx_papers_normalized_title ON papers(normalized_title);
CREATE INDEX IF NOT EXISTS idx_papers_year ON papers(year);

CREATE TABLE IF NOT EXISTS paper_summaries (
    paper_id TEXT PRIMARY KEY,
    one_sentence_summary TEXT NOT NULL,
    research_background TEXT,
    research_problem TEXT,
    objectives TEXT,
    data_sources TEXT,
    methods TEXT,
    key_findings TEXT,
    contributions TEXT,
    limitations TEXT,
    future_work TEXT,
    relation_to_seed TEXT,
    relevance_score REAL,
    summary_confidence REAL,
    summary_level TEXT NOT NULL,
    raw_llm_output TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (paper_id) REFERENCES papers(paper_key) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_paper_summaries_relevance ON paper_summaries(relevance_score);

CREATE TABLE IF NOT EXISTS project_papers (
    project_id TEXT NOT NULL,
    paper_key TEXT NOT NULL,
    depth INTEGER,
    direction TEXT,
    PRIMARY KEY (project_id, paper_key),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
    FOREIGN KEY (paper_key) REFERENCES papers(paper_key) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS citations (
    project_id TEXT NOT NULL,
    source_key TEXT NOT NULL,
    target_key TEXT NOT NULL,
    discovered_via TEXT NOT NULL DEFAULT 'manual',
    raw_json TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (project_id, source_key, target_key),
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE,
    FOREIGN KEY (source_key) REFERENCES papers(paper_key) ON DELETE CASCADE,
    FOREIGN KEY (target_key) REFERENCES papers(paper_key) ON DELETE CASCADE,
    CHECK (source_key <> target_key)
);

CREATE INDEX IF NOT EXISTS idx_citations_project_source ON citations(project_id, source_key);
CREATE INDEX IF NOT EXISTS idx_citations_project_target ON citations(project_id, target_key);

CREATE TABLE IF NOT EXISTS api_cache (
    service TEXT NOT NULL,
    cache_key TEXT NOT NULL,
    status_code INTEGER NOT NULL,
    response_body TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (service, cache_key)
);

CREATE INDEX IF NOT EXISTS idx_api_cache_expires_at ON api_cache(expires_at);

CREATE TABLE IF NOT EXISTS crawl_runs (
    run_id TEXT PRIMARY KEY,
    project_id TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at TEXT,
    stats_json TEXT NOT NULL DEFAULT '{}',
    error_message TEXT,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS project_reports (
    project_id TEXT PRIMARY KEY,
    markdown_content TEXT NOT NULL,
    report_path TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (project_id) REFERENCES projects(project_id) ON DELETE CASCADE
);
