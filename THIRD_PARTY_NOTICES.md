# Third-party notices

File Atlas source code is licensed under the repository's [MIT License](LICENSE). The optional and bundled components below retain their own licenses.

## OpenCV YuNet face detector

- Source: [OpenCV Zoo — YuNet](https://github.com/opencv/opencv_zoo/tree/main/models/face_detection_yunet)
- Pinned OpenCV Zoo commit: `47534e27c9851bb1128ccc0102f1145e27f23f98`
- Model: `face_detection_yunet_2023mar.onnx`
- SHA-256: `8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4`
- License: MIT

The Windows packaging script downloads the model and the exact upstream license from this pinned commit, verifies both files, and places them in the packaged application. The model is not committed to this repository.

## OpenCV SFace face recognizer

- Source: [OpenCV Zoo — SFace](https://github.com/opencv/opencv_zoo/tree/main/models/face_recognition_sface)
- Pinned OpenCV Zoo commit: `47534e27c9851bb1128ccc0102f1145e27f23f98`
- Model: `face_recognition_sface_2021dec.onnx`
- SHA-256: `0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79`
- License file in its upstream model directory: Apache License 2.0

The Windows packaging script downloads the model and the exact upstream license from this pinned commit, verifies both files, and places them in the packaged application. The model is not committed to this repository.

## Runtime libraries

- [OpenCV](https://opencv.org/) / `opencv-python-headless` — Apache License 2.0
- [NumPy](https://numpy.org/) — BSD-3-Clause
- [Pillow](https://python-pillow.org/) — HPND License
- [CPython](https://www.python.org/) — Python Software Foundation License and included notices
- Tcl/Tk used by the native folder picker — Tcl/Tk license terms
- [PyInstaller](https://pyinstaller.org/) bootloader — GPL with the bootloader exception
- React, React DOM and Scheduler — MIT License
- Lucide React — ISC License, with included icon attribution notices
- [FFmpeg](https://ffmpeg.org/) when supplied by the user — its build's applicable LGPL/GPL terms

The Windows release ZIP includes a `licenses/` directory containing the actual installed runtime license texts, both model licenses, and the frontend runtime notices. `licenses/runtime-manifest.json` records the application, Python and package versions used for that build. The Tcl 8.6 license copy comes from the upstream [core-8-6-15 license](https://github.com/tcltk/tcl/blob/core-8-6-15/license.terms); Tk's license is copied from the Python installation used to build the native folder picker.

The application never downloads these runtime libraries while it is running. FFmpeg and ExifTool are not bundled. Review the included license texts for the terms applying to each component.

## Optional HEIC/HEIF support in source installations

The official Windows EXE and ZIP deliberately exclude `pillow-heif` and its compiled extension, even if the build machine has it installed. HEIC/HEIF face indexing remains available when running from a source installation with the `heif` extra; the Windows EXE can use a user-provided FFmpeg for supported media preview conversions.

The `pillow-heif` Python wrapper has a BSD-3-Clause license, but that is not the complete license description of its binary wheels. The upstream [v1.8.1 bundled-library notice](https://github.com/bigcat88/pillow_heif/blob/v1.8.1/LICENSES_bundled.txt) identifies GPLv2 binary wheels containing libheif and libde265 under LGPLv3, x265 under GPLv2, and additional Windows runtime notices. Users distributing their own builds with this optional decoder must account for the actual wheel's included components and terms. It is not part of the official binary distribution.

## Why the supplied InsightFace model was not reused

The contributed `face_photo_index.py` used the public InsightFace `buffalo_l` model. InsightFace's [official model-zoo documentation](https://github.com/deepinsight/insightface/blob/master/python-package/docs/model_zoo.md) states that its public pretrained models are limited to non-commercial research unless separately licensed. File Atlas therefore uses the OpenCV Zoo models listed above and keeps the face backend replaceable through a local model directory.
