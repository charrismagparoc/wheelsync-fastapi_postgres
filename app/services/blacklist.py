import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import BlacklistRecord, Profile

# Pardz's printed blacklist (area, source, name, reason), in the list's order. Each page is loaded once
# (services/company_data.py); after that the admin manages the list on the Blacklist page.
DEFAULT_RECORDS = [
    ("CDO", "RN", "Abdullah Amunto", "Rent Sangla"),
    ("CEB", "RN", "Abegail Eusebio Sabado", "Rent Sangla"),
    ("CDO", "RN", "Abram S. Del Bando", "Payment Issue"),
    ("CEB", "RN", "Abu Shabab", "Scammer"),
    ("CDO", "RN", "Ace Constantine L. Fabiana", "Identity Theft"),
    ("CDO", "RN", "Adrian Gabriel R. Sabunod", "Payment Issue"),
    ("CDO", "RN", "Adrian Jasper A. Cocon", "Payment Issue"),
    ("CDO", "RN", "Adrian Joseph Macamay", "Bad Renter"),
    ("CDO", "RN", "Ahmeddeedath S. Salahuddin", "Bad Renter"),
    ("CDO", "FB", "Ai Ai Banaag", "Sub-Lease"),
    ("DVO", "RN", "Aida Medalla Navarro", "Bad Renter"),
    ("CDO", "FB", "Aij Villadores", "Bad Renter"),
    ("CDO", "RN", "Aime Conception A. Reyes", "Payment Issue"),
    ("CEB", "RN", "Ained Alriandy Desquitado", "Payment Issue"),
    ("CDO", "RN", "Aissa Rebecca V. Rivera", "Bad Renter"),
    ("CEB", "RN", "Akil Banto Mangontra", "Payment Issue"),
    ("CDO", "FB", "Akin LC", "Payment Issue"),
    ("CDO", "RN", "Alamina Salic Maruhom", "Rent Sangla"),
    ("DVO", "RN", "Alberto Berdera Jr.", "Bad Renter"),
    ("CEB", "RN", "Alejandro Espelete", "Scammer"),
    ("CDO", "RN", "Alex Delos Reyes", "Bad Renter"),
    ("CEB", "RN", "Alexa Marie Adolfo Estonilo", "Scammer"),
    ("CDO", "RN", "Alexander Payo Botio", "Bad Renter"),
    ("CDO", "FB", "Alexia Ruiz", "Bad Renter"),
    ("DVO", "RN", "Alexis Herana", "Bad Renter"),
    ("CDO", "FB", "Ali Macarampat", "Payment Issue"),
    ("DVO", "RN", "Ali Motiva", "Bad Renter"),
    ("CDO", "RN", "Alia Kristine D. Carpio", "Rent Sangla"),
    ("CDO", "RN", "Aljami Indasan Mayasa", "Bad Renter"),
    ("CDO", "RN", "Al-Jhunaid I. Adam", "Drug Related"),
    ("CDO", "RN", "Allan M. Egas", "Bad Renter"),
    ("CDO", "RN", "Allan P. Claves", "Bad Renter"),
    ("DVO", "RN", "Allan Pontillas", "Bad Renter"),
    ("CDO", "FB", "Allego Pau", "Scammer"),
    ("CDO", "RN", "Almanar G. Datuimam", "Bad Renter"),
    ("CDO", "RN", "Alneil Dale L. Lopez", "Bad Renter"),
    ("CDO", "RN", "Alvie John U. Moralde", "Bad Renter"),
    ("CDO", "RN", "Alvin Salic Baunto", "Payment Issue"),
    ("CDO", "FB", "Alyssa Anne Magrina", "Bad Renter"),
    ("CEB", "RN", "Amante Marlon Heponia", "Payment Issue"),
    ("CDO", "RN", "Amrani Ignalig Macaumbos", "Bad Renter"),
    ("CEB", "RN", "Amy Leah Munar", "Scammer"),
    ("CDO", "RN", "Amy Tianchon Aleroso", "Scammer"),
    ("CEB", "RN", "Anas Ramadan Salman", "Scammer"),
    ("CDO", "RN", "Andrea C. Lonoy", "Drug Related"),
    ("CDO", "RN", "Andrey G. Toreno", "Bad Renter"),
    ("CDO", "FB", "Andy Sho Es", "Bad Renter"),
    ("CEB", "RN", "Angela Lee", "Rent Tangay"),
]

# Second page of the printed list (rows 51-98).
SECOND_PAGE_RECORDS = [
    ("CDO", "RN", "Angelli O. Gaid", "Payment Issue"),
    ("CDO", "RN", "Angelo Bilar", "Bad Renter"),
    ("CDO", "RN", "Anika Ara Bernadette D. De Asis", "Bad Renter"),
    ("CDO", "RN", "Ann A. Aboda", "Bad Renter"),
    ("CEB", "RN", "Anna Kristina Donaire Harrisson", "Payment Issue"),
    ("CEB", "RN", "Anna Mae P. Acabal", "Payment Issue"),
    ("CEB", "RN", "Anna Marie Ilaida", "Payment Issue"),
    ("CDO", "RN", "Annabelle C. Rivera", "Bad Renter"),
    ("CDO", "RN", "Annabelle T. Viscayno", "Payment Issue"),
    ("CEB", "RN", "Annelyn Matiling Crismundo", "Carnaper"),
    ("CEB", "RN", "Annester Misa Aquila", "Carnaper"),
    ("CDO", "RN", "Anthony A. Patena", "Payment Issue"),
    ("CDO", "RN", "Anthony Apostado Lalanan", "Bad Renter"),
    ("CEB", "RN", "Antonio Bensi Pogoy Jr", "Payment Issue"),
    ("CEB", "FB", "Antonio Kho", "Payment Issue"),
    ("CDO", "RN", "Apple Jon V. Bromeo", "Bad Renter"),
    ("CEB", "RN", "Aquino T. Ravels", "Scammer"),
    ("DVO", "RN", "Ardian Aca Divinagracia", "Bad Renter"),
    ("CEB", "RN", "Arianna Navarro", "Payment Issue"),
    ("CEB", "RN", "Arlene Timcang Araga", "Carnaper"),
    ("DVO", "RN", "Arlyn Jane Canete", "Bad Renter"),
    ("CDO", "FB", "Arman Laconde", "Bad Renter"),
    ("CDO", "RN", "Arnel C. Pernales", "Bad Renter"),
    ("CEB", "RN", "Arnel Gomez Yburan", "Scammer"),
    ("CDO", "RN", "Arnie Vi R. Capistrano", "Payment Issue"),
    ("CDO", "RN", "Arniel M. Saa", "Payment Issue"),
    ("CDO", "RN", "Arnold D. Imperio", "Bad Renter"),
    ("DVO", "RN", "Arnold Diaz", "Bad Renter"),
    ("CDO", "RN", "Arnold G. Hallasgo", "Payment Issue"),
    ("CDO", "RN", "Arnulfo D. Lee Jr", "Rent Tangay"),
    ("CDO", "FB", "Arriane Celda", "Bad Renter"),
    ("DVO", "RN", "Arthuro Peter Pavia", "Bad Renter"),
    ("CDO", "RN", "Assiong Becoy Tomaroy", "Bad Renter"),
    ("CEB", "RN", "Audray Jell Colina Polinar", "Scammer"),
    ("CDO", "FB", "Axen Halls", "Bad Renter"),
    ("CDO", "FB", "Axle Espinosa", "Bad Renter"),
    ("CDO", "FB", "Ay Gnay", "Bad Renter"),
    ("CDO", "FB", "Aya Doblas", "Bad Renter"),
    ("CDO", "FB", "Baligya Ni Dovie", "Bad Renter"),
    ("CEB", "RN", "Barsana Bugner", "Scammer"),
    ("CEB", "RN", "Bel Fabian", "Payment Issue"),
    ("CDO", "RN", "Beliardo G. Rizares Jr.", "Bad Renter"),
    ("CDO", "FB", "Bella Zlag", "Payment Issue"),
    ("CEB", "RN", "Benedict C Gorgonio", "Payment Issue"),
    ("CEB", "RN", "Benedict C. Gorgonio", "Payment Issue"),
    ("CDO", "RN", "Benedicto Roxas", "Bad Renter"),
    ("CDO", "RN", "Benedicto T. Alenton", "Bad Renter"),
    ("DVO", "RN", "Benidict Canuto", "Bad Renter"),
]

SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}


def add_records(db: Session, rows: list[tuple]) -> None:
    db.add_all(
        BlacklistRecord(area=area, source=source, full_name=name, reason=reason)
        for area, source, name, reason in rows
    )


def name_tokens(name: str | None) -> list[str]:
    """Lower-case name parts without punctuation, middle initials or Jr./Sr.: 'Al-Jhunaid I. Adam' -> ['aljhunaid', 'adam']."""
    words = re.sub(r"[^a-z\s]", "", (name or "").lower().replace("-", "")).split()
    return [w for w in words if len(w) > 1 and w not in SUFFIXES]


def names_match(record: list[str], person: list[str]) -> bool:
    """Same first and last name, or every part of the shorter name appears in the longer one
    ('Abegail Sabado' matches 'Abegail Eusebio Sabado'). Needs at least two parts on both sides."""
    if len(record) < 2 or len(person) < 2:
        return False
    if record[0] == person[0] and record[-1] == person[-1]:
        return True
    shorter, longer = sorted((record, person), key=len)
    return shorter[0] == longer[0] and set(shorter) <= set(longer)


def normalize_id(value: str | None) -> str:
    """ID numbers compared without spaces, dashes or letter case: 'n01-23 456789' -> 'N0123456789'."""
    return re.sub(r"[^A-Za-z0-9]", "", value or "").upper()


def customer_ids(customer: Profile) -> set[str]:
    return {n for n in (normalize_id(customer.id_number), normalize_id(customer.second_id_number)) if n}


def _records_with_ids(db: Session) -> list[BlacklistRecord]:
    return db.scalars(select(BlacklistRecord).where(BlacklistRecord.id_number.is_not(None))).all()


def record_with_id(db: Session, id_number: str | None, *, exclude=None) -> BlacklistRecord | None:
    """The record that already has this ID number (blacklist ID numbers are unique)."""
    wanted = normalize_id(id_number)
    if not wanted:
        return None
    return next((r for r in _records_with_ids(db) if r.id != exclude and normalize_id(r.id_number) == wanted), None)


def id_blacklist_record(db: Session, customer: Profile) -> BlacklistRecord | None:
    """The blacklist record whose ID number is one of the customer's two IDs. Such a customer cannot book."""
    ids = customer_ids(customer)
    if not ids:
        return None
    return next((r for r in _records_with_ids(db) if normalize_id(r.id_number) in ids), None)


def blacklist_matches(db: Session, customers: list[Profile]) -> dict:
    """{customer id: (record, "id_number" or "name")}. An ID number match is certain; a name match only
    means the admin should check (two people can share a name)."""
    all_records = db.scalars(select(BlacklistRecord)).all()
    by_id = {normalize_id(r.id_number): r for r in all_records if normalize_id(r.id_number)}
    by_name = [(name_tokens(r.full_name), r) for r in all_records]
    found = {}
    for customer in customers:
        record = next((by_id[n] for n in customer_ids(customer) if n in by_id), None)
        if record is not None:
            found[customer.id] = (record, "id_number")
            continue
        person = name_tokens(customer.full_name)
        record = next((r for tokens, r in by_name if names_match(tokens, person)), None)
        if record is not None:
            found[customer.id] = (record, "name")
    return found
