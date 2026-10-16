-- News articles, the events they cluster into, and the intelligence inbox.
--
-- Articles are stored so deduplication can span runs: an article seen
-- yesterday must not become a new event tonight because the process
-- restarted. Events are the unit a person reads. Alerts are what survived
-- scoring and earned a place in the inbox.

create table mi_news_articles (
    article_id    varchar(36)  primary key,
    -- Hash of the normalised title. Two rows with the same fingerprint are
    -- the same story from different outlets.
    fingerprint   varchar(40)  not null,
    title         text         not null,
    url           text         not null default '',
    source        varchar(160) not null default '',
    source_class  varchar(28)  not null default 'other_publication',
    -- When it was published, not when we fetched it. Freshness decay reads
    -- the first; provider health reads the second.
    published_at  timestamp    not null,
    retrieved_at  timestamp    not null,
    summary       text         not null default '',
    symbols       text         not null default '',
    -- Set when this article was collapsed into another. Kept rather than
    -- deleted so the Data page can say "fifty articles, eight stories"
    -- instead of quietly showing eight.
    duplicate_of  varchar(36),
    event_id      varchar(40)
);

create index ix_mi_news_articles_fingerprint on mi_news_articles(fingerprint);
create index ix_mi_news_articles_published on mi_news_articles(published_at);
create index ix_mi_news_articles_event on mi_news_articles(event_id);

create table mi_news_events (
    event_id      varchar(40)  primary key,
    key_title     text         not null,
    symbols       text         not null default '',
    first_seen    timestamp    not null,
    last_seen     timestamp    not null,
    article_count integer      not null default 1,
    source_count  integer      not null default 1,
    best_source_class varchar(28) not null default 'other_publication',
    confirmation  varchar(20)  not null default 'credible_report',
    -- Which financial dimensions it plausibly touches, comma separated.
    -- Read whole, for display and for the half-life; never queried by
    -- element, so a join table would be structure without purpose.
    dimensions    text         not null default '',
    sentiment_label varchar(12) not null default 'neutral',
    updated_at    timestamp    not null
);

create index ix_mi_news_events_last_seen on mi_news_events(last_seen);

-- The intelligence inbox. An alert is an event that earned attention for a
-- particular portfolio, so the same event can matter to one portfolio and
-- not another -- which is the whole point of weighting by position size.
create table mi_alerts (
    alert_id      varchar(36)  primary key,
    portfolio_id  varchar(36)  not null references mi_portfolios(portfolio_id),
    event_id      varchar(40),
    -- info | watch | important | critical
    severity      varchar(12)  not null default 'info',
    title         text         not null,
    body          text         not null default '',
    symbols       text         not null default '',
    -- Every component of the attention score, stored rather than just the
    -- product. A score nobody can decompose is a score nobody can argue
    -- with, and this one will be wrong sometimes.
    score         varchar(24)  not null default '0',
    explanation   text         not null default '',
    created_at    timestamp    not null,
    read_at       timestamp,
    archived_at   timestamp,
    muted         boolean      not null default false
);

create index ix_mi_alerts_inbox on mi_alerts(portfolio_id, created_at);
create index ix_mi_alerts_event on mi_alerts(event_id);

-- Per-security monitoring intent. Watchlists are separate from holdings on
-- purpose: something you are considering deserves attention without
-- pretending you own it.
create table mi_watchlist_entries (
    portfolio_id  varchar(36) not null references mi_portfolios(portfolio_id),
    symbol        varchar(20) not null,
    -- A | B | C | D  -- owned, priority, normal, universe
    tier          varchar(2)  not null default 'C',
    note          text        not null default '',
    created_at    timestamp   not null,
    primary key (portfolio_id, symbol)
);

--@postgres-only-begin
alter table mi_news_articles     enable row level security;
alter table mi_news_events       enable row level security;
alter table mi_alerts            enable row level security;
alter table mi_watchlist_entries enable row level security;

create policy akmarkets_app_rw on mi_news_articles
    for all to akmarkets_app using (true) with check (true);
create policy akmarkets_app_rw on mi_news_events
    for all to akmarkets_app using (true) with check (true);
create policy akmarkets_app_rw on mi_alerts
    for all to akmarkets_app using (true) with check (true);
create policy akmarkets_app_rw on mi_watchlist_entries
    for all to akmarkets_app using (true) with check (true);

grant select, insert, update, delete on mi_news_articles     to akmarkets_app;
grant select, insert, update, delete on mi_news_events       to akmarkets_app;
grant select, insert, update, delete on mi_alerts            to akmarkets_app;
grant select, insert, update, delete on mi_watchlist_entries to akmarkets_app;

revoke all on mi_news_articles     from anon, authenticated;
revoke all on mi_news_events       from anon, authenticated;
revoke all on mi_alerts            from anon, authenticated;
revoke all on mi_watchlist_entries from anon, authenticated;
--@postgres-only-end
