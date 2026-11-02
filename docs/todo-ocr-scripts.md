# To do: sign reader recognizers for Georgian, Armenian and other unread scripts

The sign reader (docs/ocr.md) carries PaddleOCR recognizers for Latin with
Chinese and Japanese, Korean, Thai, Arabic (Persian too), Cyrillic, Greek,
Devanagari, Tamil and Telugu. A map's names in any other script are counted
when the reader is planned and logged as "no recognizer for ..."; signs in
them are read only where they also carry Latin or Cyrillic text.

Where it shows (2026-11-02):

- Caucasus: Georgian 11% and Armenian 6% of the names (Tbilisi, Yerevan).
- Smaller shares elsewhere: Hebrew (West Asia), Ethiopic (Horn of Africa),
  Kannada, Bengali, Gujarati and other Indic scripts (Indian subcontinent),
  Sinhala, Khmer, Lao, Burmese (Southeast Asia, Himalayas), Tibetan.

To do:

1. Find whether a recognizer exists in a form the reader loads (an ONNX
   export of a PaddleOCR-style CTC recognizer with its character
   dictionary): PaddleOCR's newer releases first, then other open models.
   Note its licence.
2. If one exists: add it to the lock file with its scripts (docs/ocr.md,
   "Pinning and updating"), check it on real signs with tools/ocr_photos.py
   (a Tbilisi and a Yerevan set), and give Caucasus the reader again with
   `cloud/repackage_zim.py --add-ocr`.
3. If none exists: weigh fine-tuning a recognizer on synthetic Georgian and
   Armenian text (fonts and street-name lists from the map's own names).
