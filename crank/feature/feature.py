#! /usr/bin/env python
# -*- coding: utf-8 -*-
# vim:fenc=utf-8
#
# Copyright (c) 2020 Kazuhiro KOBAYASHI <root.4mac@gmail.com>
#
# Distributed under terms of the MIT license.

"""Feature extractor.

Extract features such as mlfb and mcep.
"""

import logging
from pathlib import Path

import librosa
import numpy as np
import scipy.signal as sp
import soundfile as sf
from crank.utils import convert_continuos_f0, low_cut_filter, mlfb2wavf
from sprocket.speech import FeatureExtractor, Synthesizer
from sprocket.util import HDF5

EPS = 1e-10


class Feature(object):
    def __init__(self, h5_dir, conf, spkr_conf):
        self.h5_dir = Path(h5_dir)
        self.conf = conf
        self.sconf = spkr_conf
        self.feats = {}
        self.win = self._generate_window()

    def analyze(self, wavf, synth_flag=False):
        self.fs, x, flbl = self._open_wavf(wavf)
        assert self.fs == self.conf["fs"]

        h5f = self.h5_dir / (flbl + ".h5")
        if not h5f.exists():
            logging.info("extract: {}".format(wavf))

            # analyze world features, cf0, uv, then synthesize
            self._analyze_world_features(x)
            if synth_flag and self.conf["fftl"] != 256:
                self._synthesize_world_features(flbl)

            # analyze mlfb
            self._analyze_mlfb(wavf)
            if synth_flag:
                self._mlfb2wavf(flbl)

            # save as hdf5
            self._save_hdf5(h5f)
        else:
            logging.info("h5 file already exists: {}".format(str(h5f)))

    def _save_hdf5(self, h5f):
        h5 = HDF5(h5f, "a")
        for k, v in self.feats.items():
            h5.save(v, ext=k)
        h5.close()

    def _open_wavf(self, wavf):
        # get file name (wav/spkr/file.wav -> spkr/file)
        flbl = Path(wavf).stem
        x, fs = sf.read(str(wavf))
        x = np.array(x, dtype=np.float)
        x = low_cut_filter(x, fs, cutoff=70)
        return fs, x, flbl

    def _analyze_world_features(self, x, f0_only=False):
        feat = FeatureExtractor(
            analyzer="world",
            fs=self.conf["fs"],
            fftl=self.conf["fftl"],
            shiftms=self.conf["shiftms"],
            minf0=self.sconf["minf0"],
            maxf0=self.sconf["maxf0"],
        )
        # analyze world based features
        self.feats["f0"], self.feats["spc"], self.feats["ap"] = feat.analyze(x)
        self.feats["uv"], self.feats["cf0"] = convert_continuos_f0(self.feats["f0"])
        self.feats["lf0"] = np.log(self.feats["f0"] + EPS)
        self.feats["lcf0"] = np.log(self.feats["cf0"])
        if f0_only:
            return
        self.feats["mcep"] = feat.mcep(
            dim=self.conf["mcep_dim"], alpha=self.conf["mcep_alpha"]
        )
        self.feats["npow"] = feat.npow()

        # NOTE: 256 fft_size sometimes causes errors for codeap extraction
        if self.conf["fftl"] != 256 and self.conf["fs"] > 16000:
            self.feats["cap"] = feat.codeap()
            cap = self.feats["cap"]
            self.feats["ccap"] = np.zeros(cap.shape)
            self.feats["cap_uv"] = np.zeros(cap.shape)
            for d in range(self.feats["ccap"].shape[-1]):
                cap[np.where(cap[:, d] == max(cap[:, d])), d] = 0.0
                (
                    self.feats["cap_uv"][:, d],
                    self.feats["ccap"][:, d],
                ) = convert_continuos_f0(cap[:, d])

    def _synthesize_world_features(self, flbl):
        # constract Synthesizer class
        synthesizer = Synthesizer(
            fs=self.conf["fs"], fftl=self.conf["fftl"], shiftms=self.conf["shiftms"]
        )

        # analysis/synthesis using F0, mcep, and ap
        anasyn = synthesizer.synthesis(
            self.feats["f0"],
            self.feats["mcep"],
            self.feats["ap"],
            alpha=self.conf["mcep_alpha"],
        )
        self.feats["x_anasyn"] = np.clip(anasyn, -1.0, 1.0)
        anasynf = self.h5_dir / (flbl + "_anasyn.wav")
        sf.write(str(anasynf), anasyn, self.conf["fs"])

    def _analyze_mlfb(self, wavf):
        # read wav file as float format
        x, fs = sf.read(str(wavf))
        for win_type in self.windows.keys():
            if win_type == "hann":
                mlfb_name = "mlfb"
            else:
                mlfb_name = f"mlfb_{win_type}"
            x_stft = librosa.stft(
                x,
                n_fft=self.conf["fftl"],
                hop_length=self.conf["hop_size"],
                win_length=self.conf["fftl"],
                window=self.windows[win_type],
                pad_mode="reflect",
            )
            spc = np.abs(x_stft).T
            fmin = 0 if self.conf["fmin"] is None else self.conf["fmin"]
            fmax = fs // 2 if self.conf["fmax"] is None else self.conf["fmax"]
            mel_basis = librosa.filters.mel(
                fs, self.conf["fftl"], self.conf["mlfb_dim"], fmin, fmax
            )
            self.feats[mlfb_name] = np.log10(np.maximum(EPS, np.dot(spc, mel_basis.T)))

            if win_type == "hann":
                # self.feats["lsp"] = 10 * np.log10(spc + EPS)
                # self.feats["sp"] = spc
                self.feats["energy"] = np.log(
                    np.sqrt(np.clip(np.sum(spc ** 2, axis=1), EPS, 1 / EPS))
                )
                self.feats["energy_uv"] = np.zeros_like(self.feats["energy"])
                self.feats["energy_uv"][: len(self.feats["npow"])] += (
                    self.feats["npow"] > self.sconf["npow"]
                )
                _, self.feats["cenergy"] = convert_continuos_f0(
                    self.feats["energy"] * self.feats["energy_uv"]
                )

    def _mlfb2wavf(self, flbl):
        for win_type in self.conf["window_types"]:
            if win_type == "hann":
                feat_name = "mlfb"
            else:
                feat_name = f"mlfb_{win_type}"
            glf = self.h5_dir / (flbl + f"_{feat_name}_gl.wav")
            mlfb2wavf(
                self.feats[feat_name],
                glf,
                fs=self.conf["fs"],
                n_mels=self.conf["mlfb_dim"],
                fftl=self.conf["fftl"],
                hop_size=self.conf["hop_size"],
                fmin=self.conf["fmin"],
                fmax=self.conf["fmax"],
                plot=True,
            )

    def _generate_window(self):
        self.windows = {}
        assert "hann" in self.conf["window_types"]
        for win_type in self.conf["window_types"]:
            if win_type == "hann":
                win = sp.hann(self.conf["fftl"])
            elif win_type == "hamming":
                win = sp.hamming(self.conf["fftl"])
            elif win_type == "itu-g":
                win = itug_729_window(self.conf["fftl"])
            self.windows[win_type] = win


def itug_729_window(length):
    """ITU-G. 729 window function."""

    def cos_win(x, length):
        return np.cos((2 * np.pi * x) / (2 * length / 3 - 1))

    def hamming_win(x, length):
        return 0.54 - 0.46 * np.cos(
            (2 * np.pi * (x - length / 6)) / (5 * length / 3 - 1)
        )

    win = np.zeros(length)
    arange = np.arange(length)
    win[-(length // 6) :] = cos_win(arange[: length // 6], length)
    win[: -(length // 6)] = hamming_win(arange[length // 6 :], length)
    return win
