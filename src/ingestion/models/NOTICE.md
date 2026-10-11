# text_detection_ppocrv3.onnx

PP-OCRv3 text **detection** model (DB, multilingual), from PaddleOCR
(https://github.com/PaddlePaddle/PaddleOCR), converted to ONNX in
https://huggingface.co/monkt/paddleocr-onnx (`detection/v3/det.onnx`).
Licensed under the Apache License 2.0, like PaddleOCR.

It finds *where* text is on a page, in any script and in handwriting, without
reading it. ArchiveSys runs it through OpenCV's own DNN module (no extra
dependency, any platform) to tell text pages from photographs when scans are
added. SHA-256 prefix: ee40e80071ba3a32.
