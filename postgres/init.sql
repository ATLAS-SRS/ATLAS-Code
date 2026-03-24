-- Script per inizializzare il database per il Cold Storage

-- Creiamo la tabella per lo storico delle transazioni
CREATE TABLE transactions_history (
    -- Chiave primaria interna, auto-incrementante
    id SERIAL PRIMARY KEY,

    -- Colonne relazionali per query veloci e indicizzazione
    transaction_id VARCHAR(255) NOT NULL UNIQUE,
    "timestamp" TIMESTAMPTZ NOT NULL,
    risk_score INT,
    risk_level VARCHAR(50),

    -- Colonna documentale per il payload JSON completo
    payload JSONB NOT NULL,

    -- Data di inserimento nel DB per auditing
    created_at TIMESTAMPTZ DEFAULT NOW()
);

-- Indici per ottimizzare le performance delle query

-- Indice standard sul timestamp per query basate sul tempo
CREATE INDEX idx_transactions_history_timestamp ON transactions_history ("timestamp");

-- Indice GIN sulla colonna JSONB per query efficienti sui dati annidati
CREATE INDEX idx_transactions_history_payload_gin ON transactions_history USING GIN (payload);