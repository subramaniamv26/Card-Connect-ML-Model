-- V002: card transactions (normalised) and historical offer targeting / outcomes.
-- Customer and merchant attributes repeated in the transaction CSV are validated against the
-- master tables at import time and are not stored again here.

CREATE TABLE card_transactions (
    transaction_id          VARCHAR2(20)  NOT NULL,
    transaction_datetime    DATE          NOT NULL,   -- local time, Asia/Colombo
    customer_id             VARCHAR2(20)  NOT NULL,
    card_id                 VARCHAR2(20)  NOT NULL,
    merchant_id             VARCHAR2(20)  NOT NULL,
    transaction_type        VARCHAR2(20)  NOT NULL,
    transaction_channel     VARCHAR2(25)  NOT NULL,
    transaction_amount      NUMBER(14,2)  NOT NULL,   -- original amount, always >= 0
    transaction_currency    VARCHAR2(3)   NOT NULL,
    fx_rate_to_lkr          NUMBER(14,6)  NOT NULL,
    amount_lkr              NUMBER(16,2)  NOT NULL,   -- transaction_amount * fx_rate_to_lkr
    transaction_status      VARCHAR2(10)  NOT NULL,
    is_international        CHAR(1)       NOT NULL,
    related_transaction_id  VARCHAR2(20),             -- original transaction for REFUND / REVERSAL
    redeemed_offer_id       VARCHAR2(20),             -- outcome label, never a model input
    import_id               NUMBER,
    CONSTRAINT pk_card_transactions PRIMARY KEY (transaction_id),
    CONSTRAINT fk_txn_card_customer FOREIGN KEY (card_id, customer_id) REFERENCES cards (card_id, customer_id),
    CONSTRAINT fk_txn_merchant FOREIGN KEY (merchant_id) REFERENCES merchants (merchant_id),
    CONSTRAINT ck_txn_type CHECK (transaction_type IN ('PURCHASE','BILL_PAYMENT','RECURRING_PAYMENT',
                                                      'REFUND','REVERSAL')),
    CONSTRAINT ck_txn_status CHECK (transaction_status IN ('APPROVED','DECLINED','CANCELLED','REVERSED')),
    CONSTRAINT ck_txn_amount CHECK (transaction_amount >= 0),
    CONSTRAINT ck_txn_intl CHECK (is_international IN ('Y','N'))
);
CREATE INDEX ix_txn_customer_dt ON card_transactions (customer_id, transaction_datetime);
CREATE INDEX ix_txn_dt ON card_transactions (transaction_datetime);
CREATE INDEX ix_txn_merchant ON card_transactions (merchant_id);
CREATE INDEX ix_txn_card ON card_transactions (card_id);

-- Historical, customer-targeted brand offers exported by the source system (OFFER_xxxxxxxx).
-- They have no financial terms, so they are NOT part of the approved catalog. They are used only
-- as outcome labels (delivered -> redeemed) for the ranking model and for reporting.
CREATE TABLE historical_offer_targeting (
    offer_id                    VARCHAR2(20)  NOT NULL,
    customer_id                 VARCHAR2(20)  NOT NULL,
    recommended_category        VARCHAR2(20)  NOT NULL,
    recommended_merchant_brand  VARCHAR2(100) NOT NULL,
    offer_reason                VARCHAR2(400),
    preference_score            NUMBER(6,4),
    offer_priority              VARCHAR2(10),
    targeting_type              VARCHAR2(20)  NOT NULL,
    offer_status                VARCHAR2(10)  NOT NULL,
    offer_utilized              CHAR(3)       NOT NULL,
    merchant_id                 VARCHAR2(20),
    offer_start_date            DATE,
    offer_end_date              DATE,
    offer_received              CHAR(3),
    utilization_date            DATE,
    utilization_transaction_id  VARCHAR2(20),
    utilization_amount          NUMBER(14,2),
    row_hash                    VARCHAR2(32)  NOT NULL,
    import_id                   NUMBER,
    CONSTRAINT pk_hist_offer PRIMARY KEY (offer_id),
    CONSTRAINT fk_hist_offer_customer FOREIGN KEY (customer_id) REFERENCES customers (customer_id),
    CONSTRAINT fk_hist_offer_merchant FOREIGN KEY (merchant_id) REFERENCES merchants (merchant_id),
    CONSTRAINT ck_hist_offer_status CHECK (offer_status IN ('GENERATED','SENT','EXPIRED','REDEEMED')),
    CONSTRAINT ck_hist_offer_util CHECK (offer_utilized IN ('YES','NO'))
);
CREATE INDEX ix_hist_offer_customer ON historical_offer_targeting (customer_id);
CREATE INDEX ix_hist_offer_start ON historical_offer_targeting (offer_start_date);
