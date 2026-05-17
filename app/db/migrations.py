from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

LEGACY_TRANSACTION_COLUMNS = {
    "product_name",
    "weight",
    "price_per_kg",
    "total_price",
    "image_path",
}

NEW_TRANSACTION_COLUMNS = {
    "flow_type",
    "stage",
    "serial_no",
    "relation_name",
    "driver_name",
    "origin_tbs",
    "entry_timestamp",
    "exit_timestamp",
    "potongan_percent",
    "total_potongan_percent",
    "total_potongan_weight",
    "sampah_percent",
    "air_percent",
    "wajib_percent",
    "t_panjang_percent",
    "j_kosong_percent",
    "pengiriman_brd",
    "inbound_weight",
    "outbound_weight",
    "inbound_captured_image_path",
    "inbound_cropped_image_path",
    "inbound_crop_points_json",
    "outbound_captured_image_path",
    "outbound_cropped_image_path",
    "outbound_crop_points_json",
    "vehicle_no",
    "bruto_weight",
    "tara_weight",
    "netto_weight",
    "captured_image_path",
    "capture_timestamp",
}


def _ensure_ramps_table(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("""
                CREATE TABLE IF NOT EXISTS ramps (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(120) NOT NULL UNIQUE,
                    description VARCHAR(255),
                    is_active BOOLEAN NOT NULL DEFAULT 1,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """))


def _ensure_users_ramp_column(engine: Engine) -> None:
    inspector = inspect(engine)
    if "users" not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns("users")}
    if "ramp_id" in columns:
        return

    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE users ADD COLUMN ramp_id INTEGER"))


def _migrate_legacy_transactions(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(text("PRAGMA foreign_keys=OFF"))
        connection.execute(
            text("ALTER TABLE transactions RENAME TO transactions_legacy")
        )

        connection.execute(text("""
                CREATE TABLE transactions (
                    id INTEGER PRIMARY KEY,
                    store_id INTEGER,
                    employee_id INTEGER NOT NULL,
                    vehicle_no VARCHAR(30) NOT NULL,
                    ramp_id INTEGER,
                    bruto_weight FLOAT NOT NULL,
                    tara_weight FLOAT NOT NULL,
                    netto_weight FLOAT NOT NULL,
                    captured_image_path VARCHAR(500) NOT NULL,
                    cropped_image_path VARCHAR(500),
                    crop_points_json TEXT,
                    capture_timestamp DATETIME NOT NULL,
                    created_at DATETIME NOT NULL,
                    FOREIGN KEY(store_id) REFERENCES stores(id),
                    FOREIGN KEY(employee_id) REFERENCES users(id),
                    FOREIGN KEY(ramp_id) REFERENCES ramps(id)
                )
                """))

        connection.execute(text("""
                INSERT INTO transactions (
                    id,
                    store_id,
                    employee_id,
                    vehicle_no,
                    ramp_id,
                    bruto_weight,
                    tara_weight,
                    netto_weight,
                    captured_image_path,
                    cropped_image_path,
                    crop_points_json,
                    capture_timestamp,
                    created_at
                )
                SELECT
                    id,
                    store_id,
                    employee_id,
                    CASE
                        WHEN TRIM(COALESCE(product_name, '')) = '' THEN 'UNKNOWN'
                        ELSE TRIM(product_name)
                    END AS vehicle_no,
                    NULL AS ramp_id,
                    COALESCE(weight, 0) AS bruto_weight,
                    0 AS tara_weight,
                    COALESCE(weight, 0) AS netto_weight,
                    COALESCE(image_path, '') AS captured_image_path,
                    NULL AS cropped_image_path,
                    NULL AS crop_points_json,
                    COALESCE(created_at, CURRENT_TIMESTAMP) AS capture_timestamp,
                    COALESCE(created_at, CURRENT_TIMESTAMP) AS created_at
                FROM transactions_legacy
                """))

        connection.execute(text("DROP TABLE transactions_legacy"))
        connection.execute(text("PRAGMA foreign_keys=ON"))


def _add_missing_transaction_columns(engine: Engine, columns: set[str]) -> None:
    patch_statements = {
        "flow_type": "ALTER TABLE transactions ADD COLUMN flow_type VARCHAR(20) NOT NULL DEFAULT 'brondolan'",
        "stage": "ALTER TABLE transactions ADD COLUMN stage VARCHAR(30) NOT NULL DEFAULT 'completed'",
        "serial_no": "ALTER TABLE transactions ADD COLUMN serial_no VARCHAR(32)",
        "relation_name": "ALTER TABLE transactions ADD COLUMN relation_name VARCHAR(120)",
        "driver_name": "ALTER TABLE transactions ADD COLUMN driver_name VARCHAR(120)",
        "origin_tbs": "ALTER TABLE transactions ADD COLUMN origin_tbs VARCHAR(120)",
        "entry_timestamp": "ALTER TABLE transactions ADD COLUMN entry_timestamp DATETIME",
        "exit_timestamp": "ALTER TABLE transactions ADD COLUMN exit_timestamp DATETIME",
        "potongan_percent": "ALTER TABLE transactions ADD COLUMN potongan_percent FLOAT",
        "total_potongan_percent": "ALTER TABLE transactions ADD COLUMN total_potongan_percent FLOAT",
        "total_potongan_weight": "ALTER TABLE transactions ADD COLUMN total_potongan_weight FLOAT",
        "sampah_percent": "ALTER TABLE transactions ADD COLUMN sampah_percent FLOAT",
        "air_percent": "ALTER TABLE transactions ADD COLUMN air_percent FLOAT",
        "wajib_percent": "ALTER TABLE transactions ADD COLUMN wajib_percent FLOAT",
        "t_panjang_percent": "ALTER TABLE transactions ADD COLUMN t_panjang_percent FLOAT",
        "j_kosong_percent": "ALTER TABLE transactions ADD COLUMN j_kosong_percent FLOAT",
        "pengiriman_brd": "ALTER TABLE transactions ADD COLUMN pengiriman_brd FLOAT",
        "inbound_weight": "ALTER TABLE transactions ADD COLUMN inbound_weight FLOAT",
        "outbound_weight": "ALTER TABLE transactions ADD COLUMN outbound_weight FLOAT",
        "inbound_captured_image_path": "ALTER TABLE transactions ADD COLUMN inbound_captured_image_path VARCHAR(500)",
        "inbound_cropped_image_path": "ALTER TABLE transactions ADD COLUMN inbound_cropped_image_path VARCHAR(500)",
        "inbound_crop_points_json": "ALTER TABLE transactions ADD COLUMN inbound_crop_points_json TEXT",
        "outbound_captured_image_path": "ALTER TABLE transactions ADD COLUMN outbound_captured_image_path VARCHAR(500)",
        "outbound_cropped_image_path": "ALTER TABLE transactions ADD COLUMN outbound_cropped_image_path VARCHAR(500)",
        "outbound_crop_points_json": "ALTER TABLE transactions ADD COLUMN outbound_crop_points_json TEXT",
        "vehicle_no": "ALTER TABLE transactions ADD COLUMN vehicle_no VARCHAR(30) NOT NULL DEFAULT 'UNKNOWN'",
        "ramp_id": "ALTER TABLE transactions ADD COLUMN ramp_id INTEGER",
        "bruto_weight": "ALTER TABLE transactions ADD COLUMN bruto_weight FLOAT NOT NULL DEFAULT 0",
        "tara_weight": "ALTER TABLE transactions ADD COLUMN tara_weight FLOAT NOT NULL DEFAULT 0",
        "netto_weight": "ALTER TABLE transactions ADD COLUMN netto_weight FLOAT NOT NULL DEFAULT 0",
        "keterangan": "ALTER TABLE transactions ADD COLUMN keterangan TEXT",
        "captured_image_path": "ALTER TABLE transactions ADD COLUMN captured_image_path VARCHAR(500) NOT NULL DEFAULT ''",
        "cropped_image_path": "ALTER TABLE transactions ADD COLUMN cropped_image_path VARCHAR(500)",
        "crop_points_json": "ALTER TABLE transactions ADD COLUMN crop_points_json TEXT",
        "capture_timestamp": "ALTER TABLE transactions ADD COLUMN capture_timestamp DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP",
    }

    with engine.begin() as connection:
        for column_name, statement in patch_statements.items():
            if column_name in columns:
                continue
            connection.execute(text(statement))

        # Backfill from legacy columns when available.
        if "weight" in columns:
            connection.execute(
                text(
                    "UPDATE transactions SET bruto_weight = COALESCE(weight, bruto_weight), netto_weight = COALESCE(weight, netto_weight)"
                )
            )

        if "image_path" in columns:
            connection.execute(
                text(
                    "UPDATE transactions SET captured_image_path = CASE WHEN captured_image_path = '' THEN COALESCE(image_path, '') ELSE captured_image_path END"
                )
            )

        if "product_name" in columns:
            connection.execute(
                text(
                    "UPDATE transactions SET vehicle_no = CASE WHEN TRIM(vehicle_no) = '' OR vehicle_no = 'UNKNOWN' THEN TRIM(COALESCE(product_name, 'UNKNOWN')) ELSE vehicle_no END"
                )
            )

        if "entry_timestamp" in columns:
            connection.execute(
                text(
                    "UPDATE transactions SET entry_timestamp = COALESCE(entry_timestamp, capture_timestamp)"
                )
            )

        if "serial_no" in columns:
            connection.execute(
                text(
                    "UPDATE transactions SET serial_no = COALESCE(serial_no, 'LEGACY-' || id)"
                )
            )


def run_schema_migrations(engine: Engine) -> None:
    _ensure_ramps_table(engine)
    _ensure_users_ramp_column(engine)

    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "transactions" not in tables:
        return

    columns = {column["name"] for column in inspector.get_columns("transactions")}
    has_legacy = LEGACY_TRANSACTION_COLUMNS.issubset(columns)
    has_new = NEW_TRANSACTION_COLUMNS.issubset(columns)

    if has_legacy and not has_new:
        _migrate_legacy_transactions(engine)
        return

    if not has_new:
        _add_missing_transaction_columns(engine, columns)
