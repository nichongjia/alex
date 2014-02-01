#!/usr/bin/env python
# -*- coding: utf-8 -*-
# author: Ondrej Platek
from __future__ import unicode_literals

from alex.components.asr.base import ASRInterface
from alex.components.asr.utterance import UtteranceNBList, Utterance
from alex.components.asr.exceptions import KaldiSetupException
from pykaldi.utils import wst2dict, lattice_to_nbest
try:
    from pykaldi.decoders import PyGmmLatgenWrapper
except ImportError as e:
    # FIXME PYTHONPATH I can change : sys.path insert into(0,)
    raise KaldiSetupException('%s\nTry setting PYTHONPATH or LD_LIBRARY_PATH' % e.message)
import time
import os


class KaldiASR(ASRInterface):

    """ Wraps Kaldi lattice decoder,

    which firstly decodes in forward direction and generate on demand lattice
    by traversing pruned decoding graph backwards.
    """

    def __init__(self, cfg):
        super(KaldiASR, self).__init__(cfg)
        kcfg = self.cfg['ASR']['Kaldi']
        if os.path.isfile(kcfg['silent_phones']):
            # replace the path of the file with its content
            with open(kcfg['silent_phones'], 'r') as r:
                kcfg['silent_phones'] = r.read()

        self.wst = wst2dict(kcfg['wst'])
        self.max_dec_frames = kcfg['max_dec_frames']
        self.n_best = kcfg['n_best']

        # specify all other options in config
        argv = ("--config=%(config)s --verbose=%(verbose)d %(extra_args)s "
                "%(model)s %(hclg)s %(silent_phones)s" % kcfg)
        argv = argv.split()
        with open(kcfg['config']) as r:
            conf_opt = r.read()
            self.syslog.info('argv: %s\nconfig: %s' % (argv, conf_opt))

        self.decoder = PyGmmLatgenWrapper()
        self.decoder.setup(argv)

    def flush(self):
        """
        Should reset Kaldi in order to be ready for next recognition task
        :returns: self - The instance of KaldiASR
        """
        self.decoder.reset(keep_buffer_data=False)
        return self

    def rec_in(self, frame):
        """This defines asynchronous interface for speech recognition.

        Call this input function with audio data belonging into one speech segment
        that should be recognized.

        :frame: @todo
        :returns: self - The instance of KaldiASR
        """
        frame_total, start = 0, time.clock()
        self.decoder.frame_in(frame.payload)
        self.syslog.debug('frame_in of %d frames' % (len(frame.payload) / 2))
        dec_t = self.decoder.decode(max_frames=self.max_dec_frames)
        while dec_t > 0:
            frame_total += dec_t
            dec_t = self.decoder.decode(max_frames=self.max_dec_frames)
        if (frame_total > 0):
            self.syslog.debug('Forward decoding of %d frames in %s secs' % (
                frame_total, str(time.clock() - start)))
        return self

    def hyp_out(self):
        """ This defines asynchronous interface for speech recognition.
        Returns recognizers hypotheses about the input speech audio.
        """
        start = time.clock()

        # Get hypothesis
        self.decoder.prune_final()
        utt_prob, lat = self.decoder.get_lattice()
        self.decoder.reset(keep_buffer_data=False)

        # Convert lattice to nblist
        nbest = lattice_to_nbest(lat, self.n_best)
        nblist = UtteranceNBList()
        for w, word_ids in nbest:
            words = u' '.join([self.wst[i] for i in word_ids])
            self.syslog.debug(words)
            nblist.add(w, Utterance(words))

        # Log
        if len(nbest) == 0:
            nblist.add(1.0, Utterance('Empty hypothesis: Kaldi __FAIL__'))

        self.syslog.info('utterance "probability" is %f' % utt_prob)
        self.syslog.debug('hyp_out: get_lattice+nbest in %s secs' % str(time.clock() - start))

        return nblist
