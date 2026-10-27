-- V005: session-private staging tables for idempotent MERGE imports, plus change hashes for the
-- catalog tables. Global temporary tables use TEMP space, not the schema's tablespace quota.

ALTER TABLE offers ADD (row_hash VARCHAR2(32));
ALTER TABLE offer_eligibility_rules ADD (row_hash VARCHAR2(32));
ALTER TABLE offer_eligibility_rules ADD CONSTRAINT uq_rule_offer_type UNIQUE (offer_id, rule_type);

CREATE GLOBAL TEMPORARY TABLE stg_customers (
    customer_id VARCHAR2(20), customer_name VARCHAR2(100), nic_no VARCHAR2(12), customer_segment VARCHAR2(30),
    customer_home_city VARCHAR2(60), customer_home_district VARCHAR2(40), customer_home_province VARCHAR2(40),
    customer_tenure_months NUMBER(4), number_of_cards NUMBER(2), customer_contact_no VARCHAR2(20),
    mobile_device_id VARCHAR2(20), device_type VARCHAR2(20), device_os VARCHAR2(10), row_hash VARCHAR2(32)
) ON COMMIT PRESERVE ROWS;

CREATE GLOBAL TEMPORARY TABLE stg_cards (
    card_id VARCHAR2(20), customer_id VARCHAR2(20), card_network VARCHAR2(12), card_type VARCHAR2(6),
    card_tier VARCHAR2(12), card_age_months NUMBER(4), card_status VARCHAR2(10), row_hash VARCHAR2(32)
) ON COMMIT PRESERVE ROWS;

CREATE GLOBAL TEMPORARY TABLE stg_merchants (
    merchant_id VARCHAR2(20), merchant_brand VARCHAR2(100), merchant_category VARCHAR2(20),
    merchant_location VARCHAR2(200), merchant_city VARCHAR2(60), merchant_district VARCHAR2(40),
    merchant_province VARCHAR2(40), merchant_email VARCHAR2(120), merchant_contact_no VARCHAR2(20),
    merchant_type VARCHAR2(10), merchant_offer_supported CHAR(3), row_hash VARCHAR2(32)
) ON COMMIT PRESERVE ROWS;

CREATE GLOBAL TEMPORARY TABLE stg_transactions (
    transaction_id VARCHAR2(20), transaction_datetime DATE, customer_id VARCHAR2(20), card_id VARCHAR2(20),
    merchant_id VARCHAR2(20), transaction_type VARCHAR2(20), transaction_channel VARCHAR2(25),
    transaction_amount NUMBER(14,2), transaction_currency VARCHAR2(3), fx_rate_to_lkr NUMBER(14,6),
    amount_lkr NUMBER(16,2), transaction_status VARCHAR2(10), is_international CHAR(1),
    related_transaction_id VARCHAR2(20), redeemed_offer_id VARCHAR2(20)
) ON COMMIT PRESERVE ROWS;

CREATE GLOBAL TEMPORARY TABLE stg_offer_targeting (
    offer_id VARCHAR2(20), customer_id VARCHAR2(20), recommended_category VARCHAR2(20),
    recommended_merchant_brand VARCHAR2(100), offer_reason VARCHAR2(400), preference_score NUMBER(6,4),
    offer_priority VARCHAR2(10), targeting_type VARCHAR2(20), offer_status VARCHAR2(10), offer_utilized CHAR(3),
    row_hash VARCHAR2(32)
) ON COMMIT PRESERVE ROWS;

CREATE GLOBAL TEMPORARY TABLE stg_offer_outcomes (
    offer_id VARCHAR2(20), merchant_id VARCHAR2(20), offer_start_date DATE, offer_end_date DATE,
    offer_received CHAR(3), offer_utilized CHAR(3), utilization_date DATE,
    utilization_transaction_id VARCHAR2(20), utilization_amount NUMBER(14,2)
) ON COMMIT PRESERVE ROWS;

CREATE GLOBAL TEMPORARY TABLE stg_offers (
    offer_id VARCHAR2(30), offer_title VARCHAR2(150), offer_description VARCHAR2(1000),
    merchant_brand VARCHAR2(100), merchant_id VARCHAR2(20), merchant_category VARCHAR2(20),
    offer_type VARCHAR2(20), offer_value NUMBER(12,2), currency VARCHAR2(3), minimum_spend NUMBER(14,2),
    maximum_benefit NUMBER(14,2), valid_from DATE, valid_to DATE, eligible_card_networks VARCHAR2(100),
    eligible_card_types VARCHAR2(30), eligible_card_tiers VARCHAR2(100), eligible_locations VARCHAR2(1000),
    redemption_limit_per_customer NUMBER(6), total_redemption_limit NUMBER(10), campaign_budget NUMBER(16,2),
    terms_and_conditions CLOB, is_sample CHAR(1), row_hash VARCHAR2(32)
) ON COMMIT PRESERVE ROWS;

CREATE GLOBAL TEMPORARY TABLE stg_eligibility_rules (
    offer_id VARCHAR2(30), rule_type VARCHAR2(30), operator VARCHAR2(10), rule_value VARCHAR2(400),
    description VARCHAR2(400), is_active CHAR(1), row_hash VARCHAR2(32)
) ON COMMIT PRESERVE ROWS;
