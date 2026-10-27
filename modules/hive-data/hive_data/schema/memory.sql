--
-- PostgreSQL database dump
--


-- Dumped from database version 17.11 (Debian 17.11-1.pgdg12+2)
-- Dumped by pg_dump version 17.11 (Debian 17.11-1.pgdg12+2)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- The `memory` schema, as applied.
--
-- Captured with `pg_dump -s --schema=memory --no-owner --no-acl` from a
-- database built by `python -m unified_db.build_schema` in the hive_mind
-- checkout, which reads the seven live SQLite stores and so cannot be run
-- from this suite. Vendored for the same reason `core.sql` is.
--
-- The `\restrict` and `\unrestrict` lines pg_dump 17 emits are removed:
-- they are psql meta-commands, and a driver executing this file hits a
-- syntax error on the backslash.
--
-- The two extensions are prepended by hand, because a schema-restricted
-- dump omits every extension — including the one that defines the type of
-- the column this schema exists for. `vector` is NOT a trusted extension,
-- so the role applying this file must be a superuser or the extension must
-- already be present; `unaccent` is trusted.
--

-- `WITH SCHEMA public` is not decoration. The dump sets `search_path` to
-- the empty string above, so an unqualified CREATE EXTENSION has nowhere to
-- put its objects and fails with "no schema has been selected to create in".
-- `public` is also where the configuration below expects `unaccent` to be.
CREATE EXTENSION IF NOT EXISTS vector WITH SCHEMA public;
CREATE EXTENSION IF NOT EXISTS unaccent WITH SCHEMA public;

--
-- Name: memory; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA memory;


--
-- Name: SCHEMA memory; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA memory IS 'lucent: memories, knowledge graph nodes and edges, soul changes.';


--
-- Name: hive_stem; Type: TEXT SEARCH DICTIONARY; Schema: memory; Owner: -
--

CREATE TEXT SEARCH DICTIONARY memory.hive_stem (
    TEMPLATE = pg_catalog.snowball,
    language = 'english' );


--
-- Name: hive_text; Type: TEXT SEARCH CONFIGURATION; Schema: memory; Owner: -
--

CREATE TEXT SEARCH CONFIGURATION memory.hive_text (
    PARSER = pg_catalog."default" );

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR asciiword WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR word WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR numword WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR email WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR url WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR host WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR sfloat WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR version WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR hword_numpart WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR hword_part WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR hword_asciipart WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR numhword WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR asciihword WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR hword WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR url_path WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR file WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR "float" WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR "int" WITH public.unaccent, memory.hive_stem;

ALTER TEXT SEARCH CONFIGURATION memory.hive_text
    ADD MAPPING FOR uint WITH public.unaccent, memory.hive_stem;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: credentials; Type: TABLE; Schema: memory; Owner: -
--

CREATE TABLE memory.credentials (
    holder text NOT NULL,
    username text NOT NULL,
    role text NOT NULL,
    mind_id text,
    token_sha text NOT NULL,
    created_at double precision NOT NULL,
    revoked_at double precision
);


--
-- Name: edges; Type: TABLE; Schema: memory; Owner: -
--

CREATE TABLE memory.edges (
    id bigint NOT NULL,
    mind_id text NOT NULL,
    source_id bigint NOT NULL,
    target_id bigint NOT NULL,
    type text NOT NULL,
    as_of text,
    source text,
    data_class text,
    tier text,
    created_at double precision,
    properties text DEFAULT '{}'::text NOT NULL,
    owner text
);


--
-- Name: edges_id_seq; Type: SEQUENCE; Schema: memory; Owner: -
--

ALTER TABLE memory.edges ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME memory.edges_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: memories; Type: TABLE; Schema: memory; Owner: -
--

CREATE TABLE memory.memories (
    id bigint NOT NULL,
    mind_id text NOT NULL,
    content text NOT NULL,
    embedding public.vector(4096),
    tags text DEFAULT ''::text,
    source text,
    data_class text,
    tier text,
    as_of text,
    expires_at text,
    superseded bigint DEFAULT 0,
    recurring bigint,
    codebase_ref text,
    created_at bigint,
    content_search tsvector GENERATED ALWAYS AS (to_tsvector('memory.hive_text'::regconfig,
CASE
    WHEN (length(content) > 100000) THEN regexp_replace("left"(content, 100000), '\S+$'::text, ''::text)
    ELSE content
END)) STORED
);


--
-- Name: memories_id_seq; Type: SEQUENCE; Schema: memory; Owner: -
--

ALTER TABLE memory.memories ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME memory.memories_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: nodes; Type: TABLE; Schema: memory; Owner: -
--

CREATE TABLE memory.nodes (
    id bigint NOT NULL,
    mind_id text NOT NULL,
    type text NOT NULL,
    name text NOT NULL,
    first_name text,
    last_name text,
    properties text DEFAULT '{}'::text,
    data_class text,
    tier text,
    source text,
    as_of text,
    created_at double precision,
    updated_at double precision,
    owner text
);


--
-- Name: nodes_id_seq; Type: SEQUENCE; Schema: memory; Owner: -
--

ALTER TABLE memory.nodes ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME memory.nodes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: schema_edges; Type: TABLE; Schema: memory; Owner: -
--

CREATE TABLE memory.schema_edges (
    name text NOT NULL,
    source_type text NOT NULL,
    target_type text NOT NULL,
    attr_schema text NOT NULL,
    "symmetric" bigint DEFAULT 0 NOT NULL,
    created_at double precision NOT NULL
);


--
-- Name: schema_types; Type: TABLE; Schema: memory; Owner: -
--

CREATE TABLE memory.schema_types (
    name text NOT NULL,
    kind text NOT NULL,
    property_schema text NOT NULL,
    created_at double precision NOT NULL,
    CONSTRAINT schema_types_kind_check CHECK ((kind = ANY (ARRAY['first-class'::text, 'second-class'::text])))
);


--
-- Name: soul_changes; Type: TABLE; Schema: memory; Owner: -
--

CREATE TABLE memory.soul_changes (
    id bigint NOT NULL,
    node_id bigint NOT NULL,
    mind_name text NOT NULL,
    mind_id text,
    actor text NOT NULL,
    reason text NOT NULL,
    before_json text NOT NULL,
    after_json text NOT NULL,
    at text NOT NULL,
    created_at double precision NOT NULL
);


--
-- Name: soul_changes_id_seq; Type: SEQUENCE; Schema: memory; Owner: -
--

ALTER TABLE memory.soul_changes ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY (
    SEQUENCE NAME memory.soul_changes_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: credentials credentials_pkey; Type: CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.credentials
    ADD CONSTRAINT credentials_pkey PRIMARY KEY (holder);


--
-- Name: credentials credentials_token_sha_key; Type: CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.credentials
    ADD CONSTRAINT credentials_token_sha_key UNIQUE (token_sha);


--
-- Name: edges edges_pkey; Type: CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.edges
    ADD CONSTRAINT edges_pkey PRIMARY KEY (id);


--
-- Name: edges edges_source_id_target_id_type_key; Type: CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.edges
    ADD CONSTRAINT edges_source_id_target_id_type_key UNIQUE (source_id, target_id, type);


--
-- Name: memories memories_pkey; Type: CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.memories
    ADD CONSTRAINT memories_pkey PRIMARY KEY (id);


--
-- Name: nodes nodes_pkey; Type: CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.nodes
    ADD CONSTRAINT nodes_pkey PRIMARY KEY (id);


--
-- Name: schema_edges schema_edges_pkey; Type: CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.schema_edges
    ADD CONSTRAINT schema_edges_pkey PRIMARY KEY (name);


--
-- Name: schema_types schema_types_pkey; Type: CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.schema_types
    ADD CONSTRAINT schema_types_pkey PRIMARY KEY (name);


--
-- Name: soul_changes soul_changes_pkey; Type: CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.soul_changes
    ADD CONSTRAINT soul_changes_pkey PRIMARY KEY (id);


--
-- Name: memory_credentials_username; Type: INDEX; Schema: memory; Owner: -
--

CREATE INDEX memory_credentials_username ON memory.credentials USING btree (username);


--
-- Name: memory_idx_edges_mind_id; Type: INDEX; Schema: memory; Owner: -
--

CREATE INDEX memory_idx_edges_mind_id ON memory.edges USING btree (mind_id);


--
-- Name: memory_idx_edges_owner; Type: INDEX; Schema: memory; Owner: -
--

CREATE INDEX memory_idx_edges_owner ON memory.edges USING btree (owner);


--
-- Name: memory_idx_memories_content_search; Type: INDEX; Schema: memory; Owner: -
--

CREATE INDEX memory_idx_memories_content_search ON memory.memories USING gin (content_search);


--
-- Name: memory_idx_memories_data_class; Type: INDEX; Schema: memory; Owner: -
--

CREATE INDEX memory_idx_memories_data_class ON memory.memories USING btree (data_class);


--
-- Name: memory_idx_memories_expires_at; Type: INDEX; Schema: memory; Owner: -
--

CREATE INDEX memory_idx_memories_expires_at ON memory.memories USING btree (expires_at);


--
-- Name: memory_idx_memories_mind_id; Type: INDEX; Schema: memory; Owner: -
--

CREATE INDEX memory_idx_memories_mind_id ON memory.memories USING btree (mind_id);


--
-- Name: memory_idx_nodes_first_name; Type: INDEX; Schema: memory; Owner: -
--

CREATE INDEX memory_idx_nodes_first_name ON memory.nodes USING btree (first_name);


--
-- Name: memory_idx_nodes_last_name; Type: INDEX; Schema: memory; Owner: -
--

CREATE INDEX memory_idx_nodes_last_name ON memory.nodes USING btree (last_name);


--
-- Name: memory_idx_nodes_mind_id; Type: INDEX; Schema: memory; Owner: -
--

CREATE INDEX memory_idx_nodes_mind_id ON memory.nodes USING btree (mind_id);


--
-- Name: memory_idx_nodes_owner; Type: INDEX; Schema: memory; Owner: -
--

CREATE INDEX memory_idx_nodes_owner ON memory.nodes USING btree (owner);


--
-- Name: memory_idx_nodes_type; Type: INDEX; Schema: memory; Owner: -
--

CREATE INDEX memory_idx_nodes_type ON memory.nodes USING btree (type);


--
-- Name: memory_idx_soul_changes_name; Type: INDEX; Schema: memory; Owner: -
--

CREATE INDEX memory_idx_soul_changes_name ON memory.soul_changes USING btree (mind_name);


--
-- Name: memory_nodes_owner_type_name; Type: INDEX; Schema: memory; Owner: -
--

CREATE UNIQUE INDEX memory_nodes_owner_type_name ON memory.nodes USING btree (owner, type, name);


--
-- Name: edges edges_mind_id_fk_minds; Type: FK CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.edges
    ADD CONSTRAINT edges_mind_id_fk_minds FOREIGN KEY (mind_id) REFERENCES core.minds(mind_id) ON DELETE RESTRICT;


--
-- Name: edges edges_source_id_fk_nodes; Type: FK CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.edges
    ADD CONSTRAINT edges_source_id_fk_nodes FOREIGN KEY (source_id) REFERENCES memory.nodes(id) ON DELETE CASCADE;


--
-- Name: edges edges_target_id_fk_nodes; Type: FK CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.edges
    ADD CONSTRAINT edges_target_id_fk_nodes FOREIGN KEY (target_id) REFERENCES memory.nodes(id) ON DELETE CASCADE;


--
-- Name: nodes nodes_type_fk_schema_types; Type: FK CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.nodes
    ADD CONSTRAINT nodes_type_fk_schema_types FOREIGN KEY (type) REFERENCES memory.schema_types(name) ON DELETE RESTRICT;


--
-- Name: schema_edges schema_edges_source_type_fk_schema_types; Type: FK CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.schema_edges
    ADD CONSTRAINT schema_edges_source_type_fk_schema_types FOREIGN KEY (source_type) REFERENCES memory.schema_types(name) ON DELETE RESTRICT;


--
-- Name: schema_edges schema_edges_target_type_fk_schema_types; Type: FK CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.schema_edges
    ADD CONSTRAINT schema_edges_target_type_fk_schema_types FOREIGN KEY (target_type) REFERENCES memory.schema_types(name) ON DELETE RESTRICT;


--
-- Name: soul_changes soul_changes_node_id_fk_nodes; Type: FK CONSTRAINT; Schema: memory; Owner: -
--

ALTER TABLE ONLY memory.soul_changes
    ADD CONSTRAINT soul_changes_node_id_fk_nodes FOREIGN KEY (node_id) REFERENCES memory.nodes(id) ON DELETE RESTRICT;


--
-- PostgreSQL database dump complete
--


