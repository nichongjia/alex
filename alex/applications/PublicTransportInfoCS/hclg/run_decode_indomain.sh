#!/bin/bash

pykaldi_dir=/ha/projects/vystadial/lib/pykaldi-kronos-build/src/pykaldi
export LD_LIBRARY_PATH=$pykaldi_dir:$LD_LIBRARY_PATH
export PYTHONPATH=$pykaldi_dir:$pykaldi_dir/pyfst:$PYTHONPATH

python download_models.py

./decode_indomain.py -c kaldi.cfg -o decode_indomain  load ../lm/reference_transcription_dev.txt 