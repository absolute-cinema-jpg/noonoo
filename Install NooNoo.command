#!/bin/bash
# Double-click to build NooNoo.app, put it in Applications and open it. Run again after updating the code.
cd "$(dirname "$0")" && ./build_app.sh --open
