"""ORM models for netfile-tracker — all 10 tables from BUILDOUT.md §3."""

from sqlalchemy import (
    Column, String, Integer, Float, Boolean, DateTime, Date,
    Text, ForeignKey, Index, UniqueConstraint,
)
from sqlalchemy.orm import relationship
from datetime import datetime, timezone
import uuid

from app.db import Base


def gen_id():
    return str(uuid.uuid4())


def utcnow():
    return datetime.now(timezone.utc)


# ─── CORE ENTITIES ───────────────────────────────────────────


class Person(Base):
    """
    Cross-project People entity. Schema-compatible with:
    - civic_media.people (person_id, canonical_name, created_at)
    - shasta_db.people (id, name, created_at)
    """
    __tablename__ = "people"

    person_id       = Column(String, primary_key=True, default=gen_id)
    canonical_name  = Column(String, nullable=False, unique=True)
    aliases         = Column(Text)          # JSON array of alternate name forms
    entity_type     = Column(String)        # individual, committee, organization, officeholder
    notes           = Column(Text)
    atlas_synced    = Column(Boolean, default=False)
    created_at      = Column(DateTime, default=utcnow)
    updated_at      = Column(DateTime, default=utcnow, onupdate=utcnow)

    filer_links     = relationship("FilerPerson", back_populates="person")
    transaction_links = relationship("TransactionPerson", back_populates="person")


class Election(Base):
    """Election cycles from the NetFile portal."""
    __tablename__ = "elections"

    election_id     = Column(String, primary_key=True, default=gen_id)
    date            = Column(Date, nullable=False)
    name            = Column(String, nullable=False)
    election_type   = Column(String)        # primary, general, special, udel
    year            = Column(Integer, nullable=False)
    netfile_election_id = Column(String, unique=True)
    total_registered = Column(Integer)
    total_ballots_cast = Column(Integer)
    turnout_percentage = Column(Float)
    results_certified = Column(Boolean, default=False)
    data_source     = Column(String)        # netfile_portal, county_csv, manual
    source_url      = Column(String)        # link to original results page
    created_at      = Column(DateTime, default=utcnow)

    candidates      = relationship("ElectionCandidate", back_populates="election")


class Filer(Base):
    """Campaign committee or candidate filer entity."""
    __tablename__ = "filers"

    filer_id        = Column(String, primary_key=True, default=gen_id)
    netfile_filer_id = Column(String, unique=True)
    local_filer_id  = Column(String)        # FPPC committee ID
    sos_filer_id    = Column(String)        # Secretary of State ID
    name            = Column(String, nullable=False)
    filer_type      = Column(String)        # candidate, measure, pac, party
    status          = Column(String)        # active, terminated
    office          = Column(String)
    jurisdiction    = Column(String)
    first_filing    = Column(Date)
    last_filing     = Column(Date)
    created_at      = Column(DateTime, default=utcnow)
    updated_at      = Column(DateTime, default=utcnow, onupdate=utcnow)

    filings         = relationship("Filing", back_populates="filer")
    person_links    = relationship("FilerPerson", back_populates="filer")
    election_links  = relationship("ElectionCandidate", back_populates="filer")

    __table_args__ = (
        Index("ix_filers_name", "name"),
        Index("ix_filers_local_id", "local_filer_id"),
    )


class Filing(Base):
    """Individual filing (Form 460, 410, 496, 497, etc.)."""
    __tablename__ = "filings"

    filing_id       = Column(String, primary_key=True, default=gen_id)
    netfile_filing_id = Column(String, unique=True, nullable=False)
    filer_id        = Column(String, ForeignKey("filers.filer_id"), nullable=False)
    form_type       = Column(String, nullable=False)
    form_name       = Column(String)
    filing_date     = Column(DateTime, nullable=False)
    period_start    = Column(Date)
    period_end      = Column(Date)
    amendment_seq   = Column(Integer, default=0)
    amends_filing   = Column(String)
    amended_by      = Column(String)
    is_efiled       = Column(Boolean, default=True)
    efiling_vendor  = Column(String)
    pdf_path        = Column(String)
    pdf_size        = Column(Integer)
    pdf_downloaded  = Column(Boolean, default=False)
    data_source     = Column(String, default="api")
    raw_data        = Column(Text)          # JSON dump of full API response
    created_at      = Column(DateTime, default=utcnow)
    updated_at      = Column(DateTime, default=utcnow, onupdate=utcnow)

    filer           = relationship("Filer", back_populates="filings")
    transactions    = relationship("Transaction", back_populates="filing")

    __table_args__ = (
        Index("ix_filings_date", "filing_date"),
        Index("ix_filings_form", "form_type"),
        Index("ix_filings_filer", "filer_id"),
        Index("ix_filings_period", "period_start", "period_end"),
    )


class Transaction(Base):
    """Individual financial transaction from a filing."""
    __tablename__ = "transactions"

    transaction_id  = Column(String, primary_key=True, default=gen_id)
    filing_id       = Column(String, ForeignKey("filings.filing_id"), nullable=False)
    schedule        = Column(String)        # A, B1, B2, C, D, E, F, G, H, I
    transaction_type = Column(String)       # monetary_contribution, nonmonetary, expenditure, etc.
    transaction_type_code = Column(String)
    entity_name     = Column(String)
    entity_type     = Column(String)        # IND, COM, OTH, PTY, SCC
    first_name      = Column(String)
    last_name       = Column(String)
    city            = Column(String)
    state           = Column(String)
    zip_code        = Column(String)
    employer        = Column(String)
    occupation      = Column(String)
    amount          = Column(Float, nullable=False)
    cumulative_amount = Column(Float)
    transaction_date = Column(Date)
    description     = Column(Text)
    memo_code       = Column(Boolean, default=False)
    amendment_flag  = Column(String)        # A=add, D=delete, blank=original
    netfile_transaction_id = Column(String)
    data_source     = Column(String, default="excel_export")
    raw_data        = Column(Text)
    created_at      = Column(DateTime, default=utcnow)

    filing          = relationship("Filing", back_populates="transactions")
    person_links    = relationship("TransactionPerson", back_populates="transaction")

    __table_args__ = (
        Index("ix_transactions_filing", "filing_id"),
        Index("ix_transactions_name", "entity_name"),
        Index("ix_transactions_date", "transaction_date"),
        Index("ix_transactions_amount", "amount"),
        Index("ix_transactions_schedule", "schedule"),
        Index("ix_transactions_type", "transaction_type"),
    )


# ─── JUNCTION TABLES ─────────────────────────────────────────


class FilerPerson(Base):
    """Links filers to People records."""
    __tablename__ = "filer_people"

    id              = Column(String, primary_key=True, default=gen_id)
    filer_id        = Column(String, ForeignKey("filers.filer_id"), nullable=False)
    person_id       = Column(String, ForeignKey("people.person_id"), nullable=False)
    role            = Column(String)        # candidate, treasurer, principal_officer, committee
    match_confidence = Column(Float)        # Matching score (0.0–1.0)
    needs_review    = Column(Boolean, default=False)  # True for medium-confidence (0.80–0.95)
    source          = Column(String, default="manual")
    created_at      = Column(DateTime, default=utcnow)

    filer           = relationship("Filer", back_populates="person_links")
    person          = relationship("Person", back_populates="filer_links")

    __table_args__ = (
        UniqueConstraint("filer_id", "person_id", "role", name="uq_filer_person_role"),
    )


class TransactionPerson(Base):
    """Links transaction entity names to People records."""
    __tablename__ = "transaction_people"

    id              = Column(String, primary_key=True, default=gen_id)
    transaction_id  = Column(String, ForeignKey("transactions.transaction_id"), nullable=False)
    person_id       = Column(String, ForeignKey("people.person_id"), nullable=False)
    match_confidence = Column(Float)
    needs_review    = Column(Boolean, default=False)  # True for medium-confidence (0.80–0.95)
    source          = Column(String, default="auto")
    created_at      = Column(DateTime, default=utcnow)

    transaction     = relationship("Transaction", back_populates="person_links")
    person          = relationship("Person", back_populates="transaction_links")

    __table_args__ = (
        UniqueConstraint("transaction_id", "person_id", name="uq_transaction_person"),
    )


class ElectionCandidate(Base):
    """Links filers to specific election cycles."""
    __tablename__ = "election_candidates"

    id              = Column(String, primary_key=True, default=gen_id)
    election_id     = Column(String, ForeignKey("elections.election_id"), nullable=False)
    filer_id        = Column(String, ForeignKey("filers.filer_id"), nullable=False)
    office_sought   = Column(String)
    candidate_name  = Column(String)        # human name when filer is a committee
    party           = Column(String)
    is_measure      = Column(Boolean, default=False)
    measure_letter  = Column(String)
    position        = Column(String)        # support, oppose (for measures)
    votes_received  = Column(Integer)
    vote_percentage = Column(Float)
    is_winner       = Column(Boolean)
    is_runoff       = Column(Boolean)
    finish_position = Column(Integer)       # 1st, 2nd, 3rd place
    incumbent       = Column(Boolean)
    result_source   = Column(String)        # county_csv, county_pdf, manual
    result_notes    = Column(Text)
    created_at      = Column(DateTime, default=utcnow)

    election        = relationship("Election", back_populates="candidates")
    filer           = relationship("Filer", back_populates="election_links")

    __table_args__ = (
        UniqueConstraint("election_id", "filer_id", name="uq_election_candidate"),
        Index("ix_ec_election", "election_id"),
        Index("ix_ec_filer", "filer_id"),
        Index("ix_ec_office", "office_sought"),
    )


# ─── SCRAPER STATE ────────────────────────────────────────────


class ScrapeLog(Base):
    """Tracks scrape runs for resumability and audit trail."""
    __tablename__ = "scrape_log"

    log_id          = Column(String, primary_key=True, default=gen_id)
    scrape_type     = Column(String, nullable=False)
    status          = Column(String, default="running")
    started_at      = Column(DateTime, default=utcnow)
    completed_at    = Column(DateTime)
    items_processed = Column(Integer, default=0)
    items_total     = Column(Integer)
    error_message   = Column(Text)
    parameters      = Column(Text)          # JSON of scrape parameters


class RssFeedState(Base):
    """Tracks last-seen RSS items to avoid re-processing."""
    __tablename__ = "rss_feed_state"

    id              = Column(String, primary_key=True, default=gen_id)
    feed_url        = Column(String, nullable=False, unique=True)
    last_guid       = Column(String)
    last_polled     = Column(DateTime)
    last_build_date = Column(String)


class WatchedFiler(Base):
    """User-defined filer names to watch for in NetFile API searches."""
    __tablename__ = "watched_filers"

    id              = Column(String, primary_key=True, default=gen_id)
    name            = Column(String, nullable=False, unique=True)
    notes           = Column(Text)
    created_at      = Column(DateTime, default=utcnow)
