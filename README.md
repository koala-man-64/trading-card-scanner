# Pokémon Trading Card Processor

This repository contains an [Azure Functions](https://learn.microsoft.com/azure/azure-functions/) project written in Python.  It includes two functions for processing images of Pokémon trading cards:

* **Blob trigger (`BlobPokemonFunction`)** – runs automatically whenever a new image is uploaded to a specified path (by default `trading-card-scanner/input/`) in your blob container.  The uploaded image is analysed and copied into a `processed/<pokemon-name>/<filename>` folder structure.
* **Timer trigger (`TimerPokemonFunction`)** – periodically scans the input folder for any images that haven't been processed yet.  This can be useful for bulk processing existing datasets or for reprocessing files on a schedule.

## How it works

Both functions share common classification logic based on the file name, but they are triggered differently:

### Blob trigger (real‑time processing)

When a new image is uploaded to the `input` folder of your blob container, the `BlobPokemonFunction` is invoked.  The function:

1. Reads the content of the uploaded blob and verifies that it is a valid image.
2. Determines which Pokémon appears on the card by analysing the file name.  It uses a list of Generation I Pokémon names and applies both exact substring and fuzzy matching.  If no suitable match is found, the image is placed under `Unknown`.
3. Uploads the image back into the same container under `processed/<pokemon-name>/<filename>` (or another root specified by `PROCESSED_ROOT`).  Existing blobs at that path are overwritten.

### Timer trigger (bulk/scheduled processing)

The `TimerPokemonFunction` runs on a CRON schedule (every five minutes by default) and performs the following actions:

1. Connects to the Azure Blob Storage container specified via environment variables.  You may either supply a full connection string (`AZURE_STORAGE_CONNECTION_STRING`) or an account name/key pair (`AZURE_STORAGE_ACCOUNT_NAME` and `AZURE_STORAGE_ACCESS_KEY`).
2. Enumerates blobs under an input prefix (`INPUT_FOLDER`, default `input`).  Only files with common image extensions are processed; other files are ignored.
3. For each image, attempts to recognise the Pokémon depicted using the same logic as the blob trigger.
4. Uploads the image back into the same container under `processed/<pokemon-name>/<original filename>` (or another root specified via `PROCESSED_ROOT`).  Existing blobs are overwritten.

Both functions log their progress at each stage so you can monitor activity via Application Insights or the function logs in the Azure portal.

## Project structure

```
trading-card-scanner/
├── TimerPokemonFunction/
│   ├── __init__.py      # Function code
│   └── function.json    # Trigger configuration (schedule)
├── host.json           # Global runtime configuration
├── requirements.txt     # Python dependencies
└── README.md            # This file
```

### Customising the trigger paths and schedules

* **Blob trigger path** – The path monitored by `BlobPokemonFunction` is specified in `BlobPokemonFunction/function.json`.  By default it is `trading-card-scanner/input/{name}`.  You can change the container name or prefix to suit your environment.  The binding uses the `AZURE_STORAGE_CONNECTION_STRING` setting for its connection.
* **Timer trigger schedule** – Defined in `TimerPokemonFunction/function.json` using CRON syntax.  The default (`"0 */5 * * * *"`) runs the function every five minutes.  Modify this value directly or override it via the Azure portal after deployment.

### Configuring environment variables

The function relies on several environment variables which should be set in the **Application Settings** of your Function App:

| Setting                        | Default value            | Purpose                                                        |
|-------------------------------|--------------------------|----------------------------------------------------------------|
| `AZURE_STORAGE_CONNECTION_STRING` | _None_                   | Full connection string to the storage account.                |
| `AZURE_STORAGE_ACCOUNT_NAME`   | _None_                   | Account name if not using a connection string.                |
| `AZURE_STORAGE_ACCESS_KEY`     | _None_                   | Access key if not using a connection string.                 |
| `AZURE_CONTAINER_NAME`         | `trading-card-scanner`    | Name of the blob container to read from and write to.        |
| `INPUT_FOLDER`                 | `input`                  | Virtual folder where source images reside.                    |
| `PROCESSED_ROOT`               | `processed`              | Root folder for processed images.                             |

At least one authentication method must be provided: either set `AZURE_STORAGE_CONNECTION_STRING` or both `AZURE_STORAGE_ACCOUNT_NAME` and `AZURE_STORAGE_ACCESS_KEY`.

## Deploying to Azure

1. **Create a Function App** in your Azure subscription if you haven't already.  Choose the Python runtime (version 3.10+ recommended) and a consumption plan or premium plan as required.
2. **Configure application settings** (see table above) with your storage account credentials and desired folder names.
3. **Deploy the code**.  You can do this in several ways:
   * Clone or fork this repository and push to the *deployment branch* of your Function App's associated GitHub repository.
   * Use the [Azure Functions Core Tools](https://learn.microsoft.com/azure/azure-functions/functions-core-tools-install) to deploy from your local machine: `func azure functionapp publish <APP_NAME>`.
   * Configure a GitHub Actions workflow (such as the one created automatically when linking your Function App to GitHub) to build and deploy the project on each commit.

## Testing locally

Before deploying, you can test the function locally using the Azure Functions Core Tools.  Install the tools, then run:

```bash
cd trading-card-scanner
func host start
```

By default the timer will trigger on its configured schedule.  You can also invoke the function manually by sending an HTTP request to the local runtime (see the Core Tools documentation).

## Notes and limitations

* The classification logic currently relies solely on the file name.  If images in your input folder do not follow a naming convention that includes the Pokémon name, classification accuracy will degrade.  Enhancing the classifier to use image recognition would require integrating additional machine‑learning models or APIs.
* The function overwrites existing blobs in the destination folder.  If you need to preserve existing files, modify the call to `upload_blob()` to check for existing blobs before writing.

---

Created by an automated assistant for the purpose of organising Pokémon trading card images in Azure Blob Storage.