import json
import math
import zlib

from .definitions import UNIT_DIMENSION
from .unit_resolver import analyze_conversion_sympy

MAGIC = b"\x4c\x43\x43\x41"

def _decode(raw: bytes) -> dict:
    """
    Decodes bytes to dict.
    Supports both binary LCCA format and plain JSON (dev mode).
    Raises ValueError if file is not a valid LCCA or JSON file.
    """
    if raw[:4] == MAGIC:
        try:
            return json.loads(zlib.decompress(raw[4:]).decode("utf-8"))
        except Exception as e:
            raise ValueError(f"Corrupt LCCA binary data: {e}")
    # Only attempt plain JSON if the content is valid UTF-8 text.
    # This prevents a defective binary file (with corrupted MAGIC) from being
    # silently misinterpreted as a readable-mode JSON file.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("Not a valid LCCA file: binary data with no LCCA magic.")
    try:
        return json.loads(text)
    except Exception:
        raise ValueError("Not a valid LCCA file.")

def _recycle_pct(v: dict) -> float:
    """Read recyclability % - checks both field names for backward compat."""
    return float(
        v.get("post_demolition_recovery_percentage")
        or v.get("recyclability_percentage")
        or 0
    )


def is_recyclable_valid(item: dict) -> bool:
    v = item.get("values", {})
    try:
        return all(
            [
                _recycle_pct(v) > 0,
                float(v.get("scrap_rate", 0) or 0) > 0,
                float(v.get("quantity", 0) or 0) > 0,
            ]
        )
    except (TypeError, ValueError):
        return False


def calc_recyclable_qty(item: dict) -> float:
    """Recyclable Qty = quantity × (recyclability% / 100)"""
    v = item.get("values", {})
    try:
        return float(v.get("quantity", 0) or 0) * (_recycle_pct(v) / 100)
    except (TypeError, ValueError):
        return 0.0


def calc_recovered_value(item: dict) -> float:
    """Recovered Value = Recyclable Qty × scrap_rate"""
    v = item.get("values", {})
    try:
        return calc_recyclable_qty(item) * float(v.get("scrap_rate", 0) or 0)
    except (TypeError, ValueError):
        return 0.0
    
_NA = {"not_available", None, ""}
_analysis_cache: dict = {}

def _cached_analysis(unit: str, carbon_denom: str, conv_factor) -> dict:
    key = (unit, carbon_denom, str(conv_factor))
    if key not in _analysis_cache:
        _analysis_cache[key] = analyze_conversion_sympy(unit, carbon_denom, conv_factor)
    return _analysis_cache[key]

def _cf_value(v: dict) -> float:
    """Return the conversion factor, defaulting to 1.0 when not explicitly set."""
    raw = v.get("conversion_factor", "not_available")
    if raw in _NA:
        return 1.0
    try:
        val = float(raw)
        return val if val > 0 else 1.0
    except (TypeError, ValueError):
        return 1.0

def is_carbon_valid(item) -> bool:
    """Valid when carbon_emission is non-zero and CF (if explicitly set) is positive."""
    v = item.get("values", {})
    # Explicitly stored CF of 0 or negative is invalid (not just suspicious)
    cf_raw = v.get("conversion_factor", "not_available")
    if cf_raw not in _NA:
        try:
            if float(cf_raw) <= 0:
                return False
        except (TypeError, ValueError):
            pass  # unparseable CF treated as not_available → default 1.0
    try:
        emission_raw = v.get("carbon_emission", "not_available")
        if emission_raw in _NA:
            return False
        return float(emission_raw) != 0
    except (TypeError, ValueError):
        return False


def calc_carbon(item: dict) -> float:
    """Carbon = quantity × conversion_factor × carbon_emission"""
    v = item.get("values", {})
    try:
        return (
            float(v.get("quantity", 0) or 0)
            * _cf_value(v)
            * float(v.get("carbon_emission", 0) or 0)
        )
    except (TypeError, ValueError):
        return 0.0


def calc_vehicle_emission(entry: dict, mat_index: dict) -> float:
    """
    Returns total_emission

    """
    v = entry.get("vehicle", {})
    r = entry.get("route", {})
    uuids = entry.get("materials", [])

    cap = float(v.get("capacity", 0) or 0)
    gross_wt = float(v.get("gross_weight", 0) or 0)
    empty_wt = float(v.get("empty_weight", max(0.0, gross_wt - cap)) or 0)
    dist = float(r.get("distance_km", 0) or 0)
    ef = float(v.get("emission_factor", 0) or 0)

    total_emission = 0.0

    if cap <= 0:
        return 0.0

    for mat_entry in uuids:
        # materials is now [{uuid, kg_factor}]
        mat_uuid = mat_entry.get("uuid") if isinstance(mat_entry, dict) else mat_entry
        kg_factor = (
            mat_entry.get("kg_factor", 1.0) if isinstance(mat_entry, dict) else 1.0
        )


        item, chunk_id, comp_name = mat_index[mat_uuid]

        if item.get("state", {}).get("in_trash", False):
            continue

        val = item.get("values", {})
        qty = float(val.get("quantity", 0) or 0)
        unit = val.get("unit", "")
        name = val.get("material_name", "")

        # Use kg_factor from transport entry (not structure conv factor)
        qty_kg = qty * kg_factor
        qty_t = qty_kg / 1000.0
        trips = math.ceil(qty_t / cap) if cap > 0 else 0

        # Loaded trip: gross_weight × dist × trips × EF
        # Empty return: empty_weight × dist × trips × EF
        emission = (gross_wt + empty_wt) * trips * dist * ef

        warns = []
        if qty <= 0:
            warns.append("⚠ Zero quantity")
        if qty_kg <= 0 and qty > 0:
            warns.append("⚠ Zero kg - check factor")
        if trips > 1000:
            warns.append(f"⚠ {trips} trips - unusually high")
        is_mass = UNIT_DIMENSION.get(unit.lower()) == "Mass"
        if not is_mass and abs(kg_factor - 1.0) < 1e-6:
            warns.append(f"⚠ 1:1 factor for {unit} - verify conversion")

        total_emission += emission

    return total_emission