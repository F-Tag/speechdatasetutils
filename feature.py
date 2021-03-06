
import numpy as np
import pysptk
import pyworld
from librosa.feature import melspectrogram, mfcc
from librosa.filters import mel
from nnmnkwii.postfilters import merlin_post_filter
from nnmnkwii.preprocessing import preemphasis
from pyreaper import reaper
from scipy import signal
from scipy.interpolate import interp1d

from .utils import make_stft_args


def wave2spec(
        wave, fs, frame_period, window,
        nperseg=None, nmels=80, preemphasis_coef=None,
        f_min=0, f_max=None, dtype='float32',
        return_t=False):

    stft_kwargs = make_stft_args(
        frame_period, fs, nperseg=nperseg, window=window)
    htk, norm = True, "slaney"

    if preemphasis_coef is not None:
        spec_wave = preemphasis(wave, preemphasis_coef)
    else:
        spec_wave = wave
    _, t, Zxx = signal.stft(
        spec_wave, **stft_kwargs)
    pspec = np.abs(Zxx)
    mspec = melspectrogram(
        sr=fs, S=pspec,
        n_mels=nmels, fmin=f_min, fmax=f_max,
        power=1.0, htk=htk, norm=norm)
    pspec = pspec.T.astype(dtype)
    mspec = mspec.T.astype(dtype)
    upsample = fs // (1000 // frame_period)
    length = (len(wave) // upsample) * upsample
    wave = wave[:length]
    mspec = mspec[:length // upsample]
    spec = pspec[:length // upsample]

    if return_t:
        return wave, spec, mspec, upsample, t
    else:
        return wave, spec, mspec, upsample


def wav2world(
        wave, fs,
        mcep_order=25, f0_smoothing=0,
        ap_smoothing=0, mcep_smoothing=0,
        frame_period=None, f0_floor=None, f0_ceil=None,
        f0_mode="harvest"):
    # setup default values
    wave = wave.astype('float64')

    frame_period = pyworld.default_frame_period \
        if frame_period is None else frame_period
    f0_floor = pyworld.default_f0_floor if f0_floor is None else f0_floor
    f0_ceil = pyworld.default_f0_ceil if f0_ceil is None else f0_ceil
    alpha = pysptk.util.mcepalpha(fs)

    # f0
    if f0_mode == "harvest":
        f0, t = pyworld.harvest(
            wave, fs,
            f0_floor=f0_floor, f0_ceil=f0_ceil,
            frame_period=frame_period)
        threshold = 0.85

    elif f0_mode == "reaper":
        _, _, t, f0, _ = reaper(
            (wave * (2**15 - 1)).astype("int16"),
            fs, frame_period=frame_period / 1000,
            do_hilbert_transform=True)
        t, f0 = t.astype('float64'), f0.astype('float64')
        threshold = 0.1

    elif f0_mode == "dio":
        _f0, t = pyworld.dio(wave, fs)
        f0 = pyworld.stonemask(wave, _f0, t, fs)
        threshold = 0.0

    else:
        raise ValueError

    # world
    sp = pyworld.cheaptrick(wave,  f0, t, fs)
    ap = pyworld.d4c(wave, f0, t, fs, threshold=threshold)

    # extract vuv from ap
    vuv_flag = (ap[:, 0] < 0.5) * (f0 > 1.0)
    vuv = vuv_flag.astype('int')

    # continuous log f0
    clf0 = np.zeros_like(f0)
    if vuv_flag.any():
        if not vuv_flag[0]:
            f0[0] = f0[vuv_flag][0]
            vuv_flag[0] = True
        if not vuv_flag[-1]:
            f0[-1] = f0[vuv_flag][-1]
            vuv_flag[-1] = True

        idx = np.arange(len(f0))
        clf0[idx[vuv_flag]] = np.log(
            np.clip(f0[idx[vuv_flag]], f0_floor / 2, f0_ceil * 2))
        clf0[idx[~vuv_flag]] = interp1d(
            idx[vuv_flag], clf0[idx[vuv_flag]]
        )(idx[~vuv_flag])

        if f0_smoothing > 0:
            clf0 = modspec_smoothing(
                clf0, 1000 / frame_period, cut_off=f0_smoothing)
    else:
        clf0 = np.ones_like(f0) * f0_floor

    # continuous coded ap
    cap = pyworld.code_aperiodicity(ap, fs)

    if ap_smoothing > 0:
        cap = modspec_smoothing(cap, 1000 / frame_period, cut_off=ap_smoothing)

    # mcep
    mcep = pysptk.mcep(sp, order=mcep_order, alpha=alpha, itype=4)

    if mcep_smoothing > 0:
        mcep = modspec_smoothing(
            mcep, 1000 / frame_period, cut_off=mcep_smoothing)

    fbin = sp.shape[1]
    return mcep, clf0, vuv, cap, sp, fbin, t


def modspec_smoothing(array, fs, cut_off=30, axis=0, fbin=11):
    if cut_off >= fs / 2:
        return array
    h = signal.firwin(fbin, cut_off, nyq=fs // 2)
    return signal.filtfilt(h, 1, array, axis)


def world2wav(
        clf0, vuv, cap, fs, fbin,
        mcep=None, sp=None, frame_period=None, mcep_postfilter=False):

    # setup
    frame_period = pyworld.default_frame_period \
        if frame_period is None else frame_period

    clf0 = np.ascontiguousarray(clf0.astype('float64'))
    vuv = np.ascontiguousarray(vuv > 0.5).astype('int')
    cap = np.ascontiguousarray(cap.astype('float64'))
    fft_len = fbin * 2 - 2
    alpha = pysptk.util.mcepalpha(fs)

    # clf0 2 f0
    f0 = np.squeeze(np.exp(clf0)) * np.squeeze(vuv)

    # cap 2 ap
    if cap.ndim != 2:
        cap = np.expand_dims(cap, 1)
    ap = pyworld.decode_aperiodicity(cap, fs, fft_len)

    # mcep 2 sp
    if sp is None:
        if mcep is None:
            raise ValueError

        else:
            mcep = np.ascontiguousarray(mcep.astype('float64'))
            if mcep_postfilter:
                mcep = merlin_post_filter(mcep, alpha)
            sp = pysptk.mgc2sp(mcep, alpha=alpha, fftlen=fft_len)
            sp = np.abs(np.exp(sp)) ** 2
    else:
        sp = np.ascontiguousarray(sp)

    wave = pyworld.synthesize(f0, sp, ap, fs, frame_period=frame_period)

    scale = np.abs(wave).max()
    if scale > 0.99:
        wave = wave / scale * 0.99

    return wave


def sp2mcep(sp, fs, order=24):
    alpha = pysptk.util.mcepalpha(fs)
    return pysptk.mcep(sp, order, alpha=alpha, itype=4)


def apply_melfb(spec, fs, n_mels=128, amin=1e-10):
    fbin = spec.shape[-1]
    n_fft = fbin * 2 - 2
    mfb = mel(fs, n_fft, n_mels=n_mels)
    spec = np.maximum(spec, amin)
    mspec = np.maximum(spec @ mfb.T, amin)
    return mspec


def spec2mfcc(spec, fs, order=24, n_mels=128, amin=1e-10):
    fbin = spec.shape[-1]

    # pre-emphasis
    _, h = signal.freqz([1.0, -0.97], 1, fbin)
    spec = spec * np.abs(h)

    # apply mel fb
    mspec = apply_melfb(spec, fs, n_mels, amin)
    mspec = 20.0 * np.log10(mspec)

    # dct
    mfc = mfcc(y=None, sr=fs, S=mspec.T, n_mfcc=order,
               dct_type=2, norm='ortho').T

    return mfc
