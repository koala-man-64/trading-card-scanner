"""
Azure Functions blob trigger that processes Pokémon trading card images.

This function is triggered each time a new image is uploaded to the
specified Blob Storage path (``<container>/input/<filename>``).  It
performs the following steps:

1. Reads the contents of the uploaded blob into memory and verifies it
   is a valid image.
2. Determines which Pokémon the card depicts by examining the file
   name.  The classification is based on a list of Generation I
   Pokémon names and uses both exact substring and fuzzy matching.
3. Writes the original image bytes back into the same container under
   ``processed/<pokemon-name>/<original filename>``.  Existing blobs
   are overwritten.

Configuration is provided via application settings:

* ``AZURE_STORAGE_CONNECTION_STRING`` – connection string used to
  authenticate when writing the processed image.  If this setting is not
  specified the trigger binding will attempt to resolve the
  connection using ``AzureWebJobsStorage`` by default.
* ``PROCESSED_ROOT`` – root path for processed images (defaults to
  ``processed``).

See the accompanying README.md for more details.
"""

import io
import logging
import os
import re

import azure.functions as func
from azure.storage.blob import BlobServiceClient
from PIL import Image
from rapidfuzz import fuzz, process
from unidecode import unidecode

# Reuse the same list of Pokémon names from the timer triggered function.
POKEMON_NAMES = [
    "Bulbasaur", "Ivysaur", "Venusaur", "Charmander", "Charmeleon", "Charizard",
    "Squirtle", "Wartortle", "Blastoise", "Caterpie", "Metapod", "Butterfree",
    "Weedle", "Kakuna", "Beedrill", "Pidgey", "Pidgeotto", "Pidgeot", "Rattata",
    "Raticate", "Spearow", "Fearow", "Ekans", "Arbok", "Pikachu", "Raichu",
    "Sandshrew", "Sandslash", "Nidoran♀", "Nidorina", "Nidoqueen", "Nidoran♂",
    "Nidorino", "Nidoking", "Clefairy", "Clefable", "Vulpix", "Ninetales",
    "Jigglypuff", "Wigglytuff", "Zubat", "Golbat", "Oddish", "Gloom",
    "Vileplume", "Paras", "Parasect", "Venonat", "Venomoth", "Diglett",
    "Dugtrio", "Meowth", "Persian", "Psyduck", "Golduck", "Mankey",
    "Primeape", "Growlithe", "Arcanine", "Poliwag", "Poliwhirl", "Poliwrath",
    "Abra", "Kadabra", "Alakazam", "Machop", "Machoke", "Machamp",
    "Bellsprout", "Weepinbell", "Victreebel", "Tentacool", "Tentacruel",
    "Geodude", "Graveler", "Golem", "Ponyta", "Rapidash", "Slowpoke",
    "Slowbro", "Magnemite", "Magneton", "Farfetch'd", "Doduo", "Dodrio",
    "Seel", "Dewgong", "Grimer", "Muk", "Shellder", "Cloyster", "Gastly",
    "Haunter", "Gengar", "Onix", "Drowzee", "Hypno", "Krabby", "Kingler",
    "Voltorb", "Electrode", "Exeggcute", "Exeggutor", "Cubone", "Marowak",
    "Hitmonlee", "Hitmonchan", "Lickitung", "Koffing", "Weezing", "Rhyhorn",
    "Rhydon", "Chansey", "Tangela", "Kangaskhan", "Horsea", "Seadra",
    "Goldeen", "Seaking", "Staryu", "Starmie", "Mr. Mime", "Scyther", "Jynx",
    "Electabuzz", "Magmar", "Pinsir", "Tauros", "Magikarp", "Gyarados",
    "Lapras", "Ditto", "Eevee", "Vaporeon", "Jolteon", "Flareon", "Porygon",
    "Omanyte", "Omastar", "Kabuto", "Kabutops", "Aerodactyl", "Snorlax",
    "Articuno", "Zapdos", "Moltres", "Dratini", "Dragonair", "Dragonite",
    "Mewtwo", "Mew"
]


def classify_pokemon(filename: str) -> str:
    """Classify a Pokémon based on the file name using simple heuristics."""
    base_name = os.path.splitext(os.path.basename(filename))[0]
    normalised = unidecode(base_name.lower())
    normalised = re.sub(r"[^a-zA-Z]+", " ", normalised).strip()
    # Exact match check
    for pokemon in POKEMON_NAMES:
        if unidecode(pokemon.lower()) in normalised:
            return pokemon
    # Fuzzy match fallback
    match, score, _ = process.extractOne(
        normalised,
        POKEMON_NAMES,
        scorer=fuzz.partial_ratio
    )
    if score >= 80:
        return match
    return "Unknown"


def main(input_blob: func.InputStream) -> None:
    """Triggered when a new blob is uploaded to the input folder."""
    # The binding exposes the blob name via the InputStream metadata.  It
    # includes the full path relative to the container, e.g.
    # "input/pikachu_card.png".
    blob_path = input_blob.name
    blob_name = os.path.basename(blob_path)
    logging.info("Processing uploaded blob '%s'", blob_path)
    try:
        blob_bytes = input_blob.read()
    except Exception as exc:
        logging.error("Failed to read blob '%s': %s", blob_path, exc)
        return
    # Verify that the uploaded file is a valid image.
    try:
        with Image.open(io.BytesIO(blob_bytes)) as img:
            img.verify()
    except Exception as exc:
        logging.warning("Uploaded blob '%s' is not a valid image: %s", blob_path, exc)
        return
    # Classify the Pokémon by filename.
    pokemon_name = classify_pokemon(blob_name)
    logging.info("Classified '%s' as '%s'", blob_name, pokemon_name)
    # Build destination path.
    processed_root = os.getenv("PROCESSED_ROOT", "processed").strip("/")
    dest_path = f"{processed_root}/{pokemon_name}/{blob_name}"
    # Use the connection string from configuration.  If not provided,
    # AzureWebJobsStorage will be used implicitly by the trigger binding, but
    # we need to specify it here explicitly to upload the processed blob.
    connection_string = os.getenv("AZURE_STORAGE_CONNECTION_STRING")
    if not connection_string:
        # Fall back to using the same connection as the trigger binding.  In
        # the Azure portal this is configured as AzureWebJobsStorage.  The
        # environment variable may not exist locally, so the upload may
        # fail when testing offline without proper configuration.
        connection_string = os.getenv("AzureWebJobsStorage")
    if not connection_string:
        logging.error("No connection string available for uploading processed blob '%s'", dest_path)
        return
    try:
        service_client = BlobServiceClient.from_connection_string(connection_string)
        # Derive the container name and path from the input blob.  The
        # InputStream.uri is of the form:
        #   https://<account>.blob.core.windows.net/<container>/input/<name>
        # We want to upload to the same container.
        container_name = input_blob.uri.split("/")[3]
        container_client = service_client.get_container_client(container_name)
        logging.info("Uploading processed image to '%s'", dest_path)
        container_client.upload_blob(name=dest_path, data=blob_bytes, overwrite=True)
        logging.info("Successfully uploaded '%s' to '%s'", blob_name, dest_path)
    except Exception as exc:
        logging.error("Failed to upload processed blob '%s': %s", dest_path, exc)