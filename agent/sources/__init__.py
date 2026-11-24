"""Each source is a separate module. A failing source never stops the others:
main.py catches SourceError (and any other exception) per source and carries on.

To add a source: create a module with a class that returns a list of models.Offer,
then call it from main.run_scan() inside its own try/except like the others.
"""


class SourceError(Exception):
    pass


AIRLINE_NAMES = {
    "IB": "Iberia", "LA": "LATAM", "JJ": "LATAM Brasil", "UX": "Air Europa", "PU": "Plus Ultra",
    "AV": "Avianca", "CM": "Copa", "KL": "KLM", "AF": "Air France", "LH": "Lufthansa",
    "AA": "American", "UA": "United", "DL": "Delta", "TP": "TAP Air Portugal", "AM": "Aeromexico",
    "H2": "Sky Airline", "LX": "SWISS", "BA": "British Airways", "AZ": "ITA Airways", "TK": "Turkish",
    "VY": "Vueling", "AR": "Aerolineas Argentinas", "G3": "GOL", "AD": "Azul", "DM": "Arajet",
}
