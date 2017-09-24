#!/usr/bin/env python
#
# Author: Qiming Sun <osirpt.sun@gmail.com>
#

import sys
import time
import copy
from functools import reduce
import numpy
import scipy.linalg
from pyscf import lib
from pyscf.lib import logger
from pyscf.mcscf import casci
from pyscf.mcscf.casci import get_fock, cas_natorb, canonicalize
from pyscf.mcscf import mc_ao2mo
from pyscf.mcscf import chkfile
from pyscf import ao2mo
from pyscf import scf
from pyscf.scf import ciah

# ref. JCP, 82, 5053;  JCP, 73, 2342

# gradients, hessian operator and hessian diagonal
def gen_g_hop(casscf, mo, u, casdm1, casdm2, eris):
    ncas = casscf.ncas
    ncore = casscf.ncore
    nocc = ncas + ncore
    nmo = mo.shape[1]

    dm1 = numpy.zeros((nmo,nmo))
    idx = numpy.arange(ncore)
    dm1[idx,idx] = 2
    dm1[ncore:nocc,ncore:nocc] = casdm1

    # part5
    jkcaa = numpy.empty((nocc,ncas))
    # part2, part3
    vhf_a = numpy.empty((nmo,nmo))
    # part1 ~ (J + 2K)
    dm2tmp = casdm2.transpose(1,2,0,3) + casdm2.transpose(0,2,1,3)
    dm2tmp = dm2tmp.reshape(ncas**2,-1)
    hdm2 = numpy.empty((nmo,ncas,nmo,ncas))
    g_dm2 = numpy.empty((nmo,ncas))
    for i in range(nmo):
        jbuf = eris.ppaa[i]
        kbuf = eris.papa[i]
        if i < nocc:
            jkcaa[i] = numpy.einsum('ik,ik->i', 6*kbuf[:,i]-2*jbuf[i], casdm1)
        vhf_a[i] =(numpy.einsum('quv,uv->q', jbuf, casdm1)
                 - numpy.einsum('uqv,uv->q', kbuf, casdm1) * .5)
        jtmp = lib.dot(jbuf.reshape(nmo,-1), casdm2.reshape(ncas*ncas,-1))
        jtmp = jtmp.reshape(nmo,ncas,ncas)
        ktmp = lib.dot(kbuf.transpose(1,0,2).reshape(nmo,-1), dm2tmp)
        hdm2[i] = (ktmp.reshape(nmo,ncas,ncas)+jtmp).transpose(1,0,2)
        g_dm2[i] = numpy.einsum('uuv->v', jtmp[ncore:nocc])
    jbuf = kbuf = jtmp = ktmp = dm2tmp = None
    vhf_ca = eris.vhf_c + vhf_a
    h1e_mo = reduce(numpy.dot, (mo.T, casscf.get_hcore(), mo))

    ################# gradient #################
    g = numpy.zeros_like(h1e_mo)
    g[:,:ncore] = (h1e_mo[:,:ncore] + vhf_ca[:,:ncore]) * 2
    g[:,ncore:nocc] = numpy.dot(h1e_mo[:,ncore:nocc]+eris.vhf_c[:,ncore:nocc],casdm1)
    g[:,ncore:nocc] += g_dm2

    def gdep1(u):
        dt = u - numpy.eye(u.shape[0])
        mo1 = numpy.dot(mo, dt)
        g = numpy.dot(h1e_mo, dt)
        g = h1e_mo + g + g.T
        g[:,nocc:] = 0
        mo_core = mo[:,:ncore]
        mo_cas = mo[:,ncore:nocc]
        dm_core = numpy.dot(mo_core, mo1[:,:ncore].T) * 2
        dm_core = dm_core + dm_core.T
        dm_cas = reduce(numpy.dot, (mo_cas, casdm1, mo1[:,ncore:nocc].T))
        dm_cas = dm_cas + dm_cas.T
        vj, vk = casscf._scf.get_jk(casscf.mol, (dm_core,dm_cas)) # first order response only
        vhfc = numpy.dot(eris.vhf_c, dt)
        vhfc = (vhfc + vhfc.T + eris.vhf_c
                + reduce(numpy.dot, (mo.T, vj[0]-vk[0]*.5, mo)))
        vhfa = numpy.dot(vhf_a, dt)
        vhfa = (vhfa + vhfa.T + vhf_a
                + reduce(numpy.dot, (mo.T, vj[1]-vk[1]*.5, mo)))
        g[:,:ncore] += vhfc[:,:ncore]+vhfa[:,:ncore]
        g[:,:ncore] *= 2
        g[:,ncore:nocc] = numpy.dot(g[:,ncore:nocc]+vhfc[:,ncore:nocc], casdm1)

        g[:,ncore:nocc] += numpy.einsum('purv,rv->pu', hdm2, dt[:,ncore:nocc])
        g[:,ncore:nocc] += numpy.dot(u.T, g_dm2)
        return g

    def gdep4(u):
        mo1 = numpy.dot(mo, u)
        g = numpy.zeros_like(h1e_mo)
        g[:,:nocc] = reduce(numpy.dot, (u.T, h1e_mo, u[:,:nocc]))
        dm_core0 = reduce(numpy.dot, (mo[:,:ncore], mo[:,:ncore].T)) * 2
        dm_core1 = reduce(numpy.dot, (mo1[:,:ncore], mo1[:,:ncore].T)) * 2
        dm_cas0  = reduce(numpy.dot, (mo[:,ncore:nocc], casdm1, mo[:,ncore:nocc].T))
        dm_cas1  = reduce(numpy.dot, (mo1[:,ncore:nocc], casdm1, mo1[:,ncore:nocc].T))
        vj, vk = casscf._scf.get_jk(casscf.mol, (dm_core1-dm_core0, dm_cas1-dm_cas0))
        vhfc1 =(reduce(numpy.dot, (mo1.T, vj[0]-vk[0]*.5, mo1[:,:nocc]))
              + reduce(numpy.dot, (u.T, eris.vhf_c, u[:,:nocc])))
        vhfa1 =(reduce(numpy.dot, (mo1.T, vj[1]-vk[1]*.5, mo1[:,:nocc]))
              + reduce(numpy.dot, (u.T, vhf_a, u[:,:nocc])))
        g[:,:ncore] += vhfc1[:,:ncore] + vhfa1[:,:ncore]
        g[:,:ncore] *= 2
        g[:,ncore:nocc] = numpy.dot(g[:,ncore:nocc]+vhfc1[:,ncore:nocc], casdm1)

        if hasattr(eris, '_paaa'):
            paaa = eris._paaa
        else:
            paaa = casscf._exact_paaa(mo, u)
        g[:,ncore:nocc] += numpy.einsum('puvw,tuvw->pt', paaa, casdm2)
        return g

    def gorb_update(u, dep4=False):
        if dep4:
            g = gdep4(u)
        else:  # DEP1/first order T-expansion
            g = gdep1(u)
        return casscf.pack_uniq_var(g-g.T)

    ############## hessian, diagonal ###########

    # part7
    h_diag = numpy.einsum('ii,jj->ij', h1e_mo, dm1) - h1e_mo * dm1
    h_diag = h_diag + h_diag.T

    # part8
    g_diag = g.diagonal()
    h_diag -= g_diag + g_diag.reshape(-1,1)
    idx = numpy.arange(nmo)
    h_diag[idx,idx] += g_diag * 2

    # part2, part3
    v_diag = vhf_ca.diagonal() # (pr|kl) * E(sq,lk)
    h_diag[:,:ncore] += v_diag.reshape(-1,1) * 2
    h_diag[:ncore] += v_diag * 2
    idx = numpy.arange(ncore)
    h_diag[idx,idx] -= v_diag[:ncore] * 4
    # V_{pr} E_{sq}
    tmp = numpy.einsum('ii,jj->ij', eris.vhf_c, casdm1)
    h_diag[:,ncore:nocc] += tmp
    h_diag[ncore:nocc,:] += tmp.T
    tmp = -eris.vhf_c[ncore:nocc,ncore:nocc] * casdm1
    h_diag[ncore:nocc,ncore:nocc] += tmp + tmp.T

    # part4
    # -2(pr|sq) + 4(pq|sr) + 4(pq|rs) - 2(ps|rq)
    tmp = 6 * eris.k_pc - 2 * eris.j_pc
    h_diag[ncore:,:ncore] += tmp[ncore:]
    h_diag[:ncore,ncore:] += tmp[ncore:].T

    # part5 and part6 diag
    # -(qr|kp) E_s^k  p in core, sk in active
    h_diag[:nocc,ncore:nocc] -= jkcaa
    h_diag[ncore:nocc,:nocc] -= jkcaa.T

    v_diag = numpy.einsum('ijij->ij', hdm2)
    h_diag[ncore:nocc,:] += v_diag.T
    h_diag[:,ncore:nocc] += v_diag

# Does this term contribute to internal rotation?
#    h_diag[ncore:nocc,ncore:nocc] -= v_diag[:,ncore:nocc]*2

    g_orb = casscf.pack_uniq_var(g-g.T)
    h_diag = casscf.pack_uniq_var(h_diag)

    def h_op(x):
        x1 = casscf.unpack_uniq_var(x)

        # part7
        # (-h_{sp} R_{rs} gamma_{rq} - h_{rq} R_{pq} gamma_{sp})/2 + (pr<->qs)
        x2 = reduce(lib.dot, (h1e_mo, x1, dm1))
        # part8
        # (g_{ps}\delta_{qr}R_rs + g_{qr}\delta_{ps}) * R_pq)/2 + (pr<->qs)
        x2 -= numpy.dot(g.T, x1)
        # part2
        # (-2Vhf_{sp}\delta_{qr}R_pq - 2Vhf_{qr}\delta_{sp}R_rs)/2 + (pr<->qs)
        x2[:ncore] += reduce(numpy.dot, (x1[:ncore,ncore:], vhf_ca[ncore:])) * 2
        # part3
        # (-Vhf_{sp}gamma_{qr}R_{pq} - Vhf_{qr}gamma_{sp}R_{rs})/2 + (pr<->qs)
        x2[ncore:nocc] += reduce(numpy.dot, (casdm1, x1[ncore:nocc], eris.vhf_c))
        # part1
        x2[:,ncore:nocc] += numpy.einsum('purv,rv->pu', hdm2, x1[:,ncore:nocc])

        if ncore > 0:
            # part4, part5, part6
# Due to x1_rs [4(pq|sr) + 4(pq|rs) - 2(pr|sq) - 2(ps|rq)] for r>s p>q,
#    == -x1_sr [4(pq|sr) + 4(pq|rs) - 2(pr|sq) - 2(ps|rq)] for r>s p>q,
# x2[:,:ncore] += H * x1[:,:ncore] => (becuase x1=-x1.T) =>
# x2[:,:ncore] += -H' * x1[:ncore] => (becuase x2-x2.T) =>
# x2[:ncore] += H' * x1[:ncore]
            va, vc = casscf.update_jk_in_ah(mo, x1, casdm1, eris)
            x2[ncore:nocc] += va
            x2[:ncore,ncore:] += vc

        # (pr<->qs)
        x2 = x2 - x2.T
        return casscf.pack_uniq_var(x2)

    return g_orb, gorb_update, h_op, h_diag

def rotate_orb_cc(casscf, mo, fcasdm1, fcasdm2, eris, x0_guess=None,
                  conv_tol_grad=1e-4, max_stepsize=None, verbose=None):
    if isinstance(verbose, logger.Logger):
        log = verbose
    else:
        log = logger.Logger(casscf.stdout, casscf.verbose)
    if max_stepsize is None:
        max_stepsize = casscf.max_stepsize

    t3m = (time.clock(), time.time())
    u = 1
    g_orb, gorb_update, h_op, h_diag = \
            casscf.gen_g_hop(mo, u, fcasdm1(), fcasdm2(), eris)
    g_kf0 = g_kf = g_orb
    norm_gkf0 = norm_gkf = norm_gorb = numpy.linalg.norm(g_orb)
    log.debug('    |g|=%5.3g', norm_gorb)
    t3m = log.timer('gen h_op', *t3m)

    def precond(x, e):
        if callable(h_diag):
            x = h_diag(x, e-casscf.ah_level_shift)
        else:
            hdiagd = h_diag-(e-casscf.ah_level_shift)
            hdiagd[abs(hdiagd)<1e-8] = 1e-8
            x = x/hdiagd
        norm_x = numpy.linalg.norm(x)
        x *= 1/norm_x
        #if norm_x < 1e-2:
        #    x *= 1e-2/norm_x
        return x

# Dynamically increase the number of micro cycles when approach convergence?
#    if norm_gorb < 0.01:
#        max_cycle = casscf.max_cycle_micro_inner-int(numpy.log10(norm_gorb+1e-9))
#    else:
#        max_cycle = casscf.max_cycle_micro_inner
    max_cycle = casscf.max_cycle_micro_inner
    dr = 0
    jkcount = 0
    norm_dr = 0
    if x0_guess is None:
        x0_guess = g_orb
    ah_conv_tol = casscf.ah_conv_tol
    #ah_start_cycle = max(casscf.ah_start_cycle, int(-numpy.log10(norm_gorb)))
    ah_start_cycle = casscf.ah_start_cycle
    while True:
        # increase the AH accuracy when approach convergence
        ah_start_tol = min(norm_gorb*casscf.ah_grad_trust_region, casscf.ah_start_tol)
        log.debug1('Set ah_start_tol %g, ah_start_cycle %d, max_cycle %d',
                   ah_start_tol, ah_start_cycle, max_cycle)
        g_orb0 = g_orb
        imic = 0

        g_op = lambda: g_orb

        for ah_end, ihop, w, dxi, hdxi, residual, seig \
                in ciah.davidson_cc(h_op, g_op, precond, x0_guess,
                                    tol=ah_conv_tol, max_cycle=casscf.ah_max_cycle,
                                    lindep=casscf.ah_lindep, verbose=log):
            # residual = v[0] * (g+(h-e)x) ~ v[0] * grad
            norm_residual = numpy.linalg.norm(residual)
            if (ah_end or ihop == casscf.ah_max_cycle or # make sure to use the last step
                ((norm_residual < ah_start_tol) and (ihop >= ah_start_cycle)) or
                (seig < casscf.ah_lindep)):
                imic += 1
                dxmax = numpy.max(abs(dxi))
                if dxmax > max_stepsize:
                    scale = max_stepsize / dxmax
                    log.debug1('... scale rotation size %g', scale)
                    dxi *= scale
                    hdxi *= scale
                else:
                    scale = None

                g_orb = g_orb + hdxi
                dr = dr + dxi
                norm_gorb0, norm_gorb = norm_gorb, numpy.linalg.norm(g_orb)
                norm_dxi = numpy.linalg.norm(dxi)
                norm_dr = numpy.linalg.norm(dr)
                log.debug('    imic %d(%d)  |g[o]|=%5.3g  |dxi|=%5.3g  '
                          'max(|x|)=%5.3g  |dr|=%5.3g  eig=%5.3g  seig=%5.3g',
                          imic, ihop, norm_gorb, norm_dxi,
                          dxmax, norm_dr, w, seig)

                if imic > 1 and norm_gorb > norm_gkf*casscf.ah_grad_trust_region:
                    break

                elif (imic >= max_cycle or norm_gorb < conv_tol_grad*.3):
                    break

        u = casscf.update_rotate_matrix(dr)
        jkcount += ihop
        gorb_update = h_op = h_diag = None
        t3m = log.timer('aug_hess in %d inner iters' % imic, *t3m)

        yield u, g_orb0, jkcount

        t3m = (time.clock(), time.time())
        g_kf1, gorb_update, h_op, h_diag = \
                casscf.gen_g_hop(mo, u, fcasdm1(), fcasdm2(), eris)
        g_kf1 = gorb_update(u, casscf.with_dep4)
        jkcount += 1

        norm_gkf1 = numpy.linalg.norm(g_kf1)
        norm_dg = numpy.linalg.norm(g_kf1-g_orb)
        log.debug('    |g|=%5.3g (keyframe), |g-correction|=%5.3g',
                  norm_gkf1, norm_dg)
#
# Special treatment if out of trust region
#
        if (norm_dg > norm_gorb*casscf.ah_grad_trust_region and
            norm_gkf1 > norm_gkf and
            norm_gkf1 > norm_gkf0*casscf.ah_grad_trust_region):
            log.debug('    Keyframe |g|=%5.3g  |g_last| =%5.3g out of trust region',
                      norm_gkf1, norm_gorb)
            if ((not casscf.with_dep4 and norm_gkf > 5e-4) or
                (hasattr(casscf._scf, 'with_df') and casscf._scf.with_df) or
                isinstance(casscf._scf, scf.uhf.UHF)):
#TODO:  transformation for DF integrals
                u = casscf.update_rotate_matrix(dr-dxi)
                yield u, g_kf-hdxi, jkcount
                break
            else:
                g_kf1 = gorb_update(u, True)
                jkcount += 1
                norm_gkf1 = numpy.linalg.norm(g_kf1)
                norm_dg = numpy.linalg.norm(g_kf1-g_orb)
                log.debug('With dep4 |g|=%5.3g (keyframe), |g-correction|=%5.3g',
                          norm_gkf1, norm_dg)
        t3m = log.timer('gen h_op', *t3m)
        g_orb = g_kf = g_kf1
        norm_gorb = norm_gkf = norm_gkf1
        x0_guess = dxi
        ah_start_cycle -= 1


def kernel(casscf, mo_coeff, tol=1e-7, conv_tol_grad=None,
           ci0=None, callback=None, verbose=logger.NOTE, dump_chk=True):
    '''CASSCF solver
    '''
    if isinstance(verbose, logger.Logger):
        log = verbose
    else:
        log = logger.Logger(casscf.stdout, verbose)
    cput0 = (time.clock(), time.time())
    log.debug('Start 1-step CASSCF')
    if callback is None:
        callback = casscf.callback

    mo = mo_coeff
    nmo = mo_coeff.shape[1]
    #TODO: lazy evaluate eris, to leave enough memory for FCI solver
    eris = casscf.ao2mo(mo)
    e_tot, e_ci, fcivec = casscf.casci(mo, ci0, eris, log, locals())
    if casscf.ncas == nmo and not casscf.internal_rotation:
        if casscf.canonicalization:
            log.debug('CASSCF canonicalization')
            mo, fcivec, mo_energy = casscf.canonicalize(mo, fcivec, eris, False,
                                                        casscf.natorb, verbose=log)
            return True, e_tot, e_ci, fcivec, mo, mo_energy

    if conv_tol_grad is None:
        conv_tol_grad = numpy.sqrt(tol)
        logger.info(casscf, 'Set conv_tol_grad to %g', conv_tol_grad)
    conv_tol_ddm = conv_tol_grad * 3
    conv = False
    totmicro = totinner = 0
    norm_gorb = norm_gci = -1
    de, elast = e_tot, e_tot
    r0 = None

    t1m = log.timer('Initializing 1-step CASSCF', *cput0)
    casdm1, casdm2 = casscf.fcisolver.make_rdm12(fcivec, casscf.ncas, casscf.nelecas)
    norm_ddm = 1e2
    casdm1_prev = casdm1_last = casdm1
    t3m = t2m = log.timer('CAS DM', *t1m)
    imacro = 0
    while not conv and imacro < casscf.max_cycle_macro:
        imacro += 1
        max_cycle_micro = casscf.micro_cycle_scheduler(locals())
        max_stepsize = casscf.max_stepsize_scheduler(locals())
        imicro = 0
        rota = casscf.rotate_orb_cc(mo, lambda:casdm1, lambda:casdm2,
                                    eris, r0, conv_tol_grad*.3, max_stepsize, log)
        for u, g_orb, njk in rota:
            imicro += 1
            norm_gorb = numpy.linalg.norm(g_orb)
            if imicro == 1:
                norm_gorb0 = norm_gorb
            norm_t = numpy.linalg.norm(u-numpy.eye(nmo))
            t3m = log.timer('orbital rotation', *t3m)
            #if norm_gci is None:
            #    accepted_gorb = norm_gorb < norm_ddm*2
            #else:
            #    accepted_gorb = norm_gorb < norm_gci*1.5
            #if (imicro >= max_cycle_micro and
            #    (accepted_gorb or imicro > max_cycle_micro*2 or
            #     norm_gorb > norm_gorb0)):
            if imicro >= max_cycle_micro:
                log.debug('micro %d  |u-1|=%5.3g  |g[o]|=%5.3g',
                          imicro, norm_t, norm_gorb)
                break

            casdm1, casdm2, gci, fcivec = casscf.update_casdm(mo, u, fcivec, e_ci, eris, locals())
            norm_ddm = numpy.linalg.norm(casdm1 - casdm1_last)
            norm_ddm_micro = numpy.linalg.norm(casdm1 - casdm1_prev)
            casdm1_prev = casdm1
            t3m = log.timer('update CAS DM', *t3m)
            if isinstance(gci, numpy.ndarray):
                norm_gci = numpy.linalg.norm(gci)
                log.debug('micro %d  |u-1|=%5.3g  |g[o]|=%5.3g  |g[c]|=%5.3g  |ddm|=%5.3g',
                          imicro, norm_t, norm_gorb, norm_gci, norm_ddm)
            else:
                norm_gci = None
                log.debug('micro %d  |u-1|=%5.3g  |g[o]|=%5.3g  |g[c]|=%s  |ddm|=%5.3g',
                          imicro, norm_t, norm_gorb, norm_gci, norm_ddm)

            if callable(callback):
                callback(locals())

            t3m = log.timer('micro iter %d'%imicro, *t3m)
            if (norm_t < conv_tol_grad or
                (norm_gorb < conv_tol_grad*.5 and
                 (norm_ddm < conv_tol_ddm*.4 or norm_ddm_micro < conv_tol_ddm*.4))):
                break

        rota.close()
        rota = None

        totmicro += imicro
        totinner += njk

        eris = None
        # keep u, g_orb in locals() so that they can be accessed by callback
        u = u.copy()
        g_orb = g_orb.copy()
        mo = casscf.rotate_mo(mo, u, log)
        eris = casscf.ao2mo(mo)
        t2m = log.timer('update eri', *t3m)

        e_tot, e_ci, fcivec = casscf.casci(mo, fcivec, eris, log, locals())
        casdm1, casdm2 = casscf.fcisolver.make_rdm12(fcivec, casscf.ncas, casscf.nelecas)
        norm_ddm = numpy.linalg.norm(casdm1 - casdm1_last)
        casdm1_prev = casdm1_last = casdm1
        log.timer('CASCI solver', *t2m)
        t3m = t2m = t1m = log.timer('macro iter %d'%imacro, *t1m)

        de, elast = e_tot - elast, e_tot
        if (abs(de) < tol
            and (norm_gorb0 < conv_tol_grad and norm_ddm < conv_tol_ddm)):
            conv = True

        if dump_chk:
            casscf.dump_chk(locals())

        if callable(callback):
            callback(locals())

        r0 = casscf.pack_uniq_var(u)

    if conv:
        log.info('1-step CASSCF converged in %d macro (%d JK %d micro) steps',
                 imacro+1, totinner, totmicro)
    else:
        log.info('1-step CASSCF not converged, %d macro (%d JK %d micro) steps',
                 imacro+1, totinner, totmicro)

    if casscf.canonicalization:
        log.info('CASSCF canonicalization')
        mo, fcivec, mo_energy = \
                casscf.canonicalize(mo, fcivec, eris, False, casscf.natorb, casdm1, log)
        if casscf.natorb: # dump_chk may save casdm1
            occ, ucas = casscf._eig(-casdm1, ncore, nocc)[0]
            casdm1 = -occ

    if dump_chk:
        casscf.dump_chk(locals())

    log.timer('1-step CASSCF', *cput0)
    return conv, e_tot, e_ci, fcivec, mo, mo_energy


# To extend CASSCF for certain CAS space solver, it can be done by assign an
# object or a module to CASSCF.fcisolver.  The fcisolver object or module
# should at least have three member functions "kernel" (wfn for given
# hamiltonain), "make_rdm12" (1- and 2-pdm), "absorb_h1e" (effective
# 2e-hamiltonain) in 1-step CASSCF solver, and two member functions "kernel"
# and "make_rdm12" in 2-step CASSCF solver
class CASSCF(casci.CASCI):
    __doc__ = casci.CASCI.__doc__ + '''CASSCF

    Extra attributes for CASSCF:

        conv_tol : float
            Converge threshold.  Default is 1e-7
        conv_tol_grad : float
            Converge threshold for CI gradients and orbital rotation gradients.
            Default is 1e-4
        max_stepsize : float
            The step size for orbital rotation.  Small step (0.005 - 0.05) is prefered.
            (see notes in max_cycle_micro_inner attribute)
            Default is 0.03.
        max_cycle_macro : int
            Max number of macro iterations.  Default is 50.
        max_cycle_micro : int
            Max number of micro iterations in each macro iteration.  Depending on
            systems, increasing this value might reduce the total macro
            iterations.  Generally, 2 - 5 steps should be enough.  Default is 3.
        max_cycle_micro_inner : int
            For the augmented hessian solver, max number of orbital rotation
            steps (controled by max_stepsize).  Value between 2 - 8 is preferred.
            Default is 4.
            Note the 1-step CASSCF algorithm prefers many small steps.  Though might
            increasing the total number of iterations, the small steps can effectively
            prevent oscillating in the total energy.  If the (macro iteration) total
            energy does not monotonically decrease, you can try to reduce max_stepsize
            and increase max_cycle_micro_inner.  For simple system, large max_stepsize
            small max_cycle_micro_inner may give the fast convergence, but it's not recommended.
        ah_level_shift : float, for AH solver.
            Level shift for the Davidson diagonalization in AH solver.  Default is 1e-8.
        ah_conv_tol : float, for AH solver.
            converge threshold for AH solver.  Default is 1e-12.
        ah_max_cycle : float, for AH solver.
            Max number of iterations allowd in AH solver.  Default is 30.
        ah_lindep : float, for AH solver.
            Linear dependence threshold for AH solver.  Default is 1e-14.
        ah_start_tol : flat, for AH solver.
            In AH solver, the orbital rotation is started without completely solving the AH problem.
            This value is to control the start point. Default is 0.2.
        ah_start_cycle : int, for AH solver.
            In AH solver, the orbital rotation is started without completely solving the AH problem.
            This value is to control the start point. Default is 2.

            ``ah_conv_tol``, ``ah_max_cycle``, ``ah_lindep``, ``ah_start_tol`` and ``ah_start_cycle``
            can affect the accuracy and performance of CASSCF solver.  Lower
            ``ah_conv_tol`` and ``ah_lindep`` might improve the accuracy of CASSCF
            optimization, but decrease the performance.
            
            >>> from pyscf import gto, scf, mcscf
            >>> mol = gto.M(atom='N 0 0 0; N 0 0 1', basis='ccpvdz', verbose=0)
            >>> mf = scf.UHF(mol)
            >>> mf.scf()
            >>> mc = mcscf.CASSCF(mf, 6, 6)
            >>> mc.conv_tol = 1e-10
            >>> mc.ah_conv_tol = 1e-5
            >>> mc.kernel()
            -109.044401898486001
            >>> mc.ah_conv_tol = 1e-10
            >>> mc.kernel()
            -109.044401887945668

        chkfile : str
            Checkpoint file to save the intermediate orbitals during the CASSCF optimization.
            Default is the checkpoint file of mean field object.
        ci_response_space : int
            subspace size to solve the CI vector response.  Default is 3.
        callback : function(envs_dict) => None
            callback function takes one dict as the argument which is
            generated by the builtin function :func:`locals`, so that the
            callback function can access all local variables in the current
            envrionment.

    Saved results

        e_tot : float
            Total MCSCF energy (electronic energy plus nuclear repulsion)
        ci : ndarray
            CAS space FCI coefficients
        converged : bool
            It indicates CASSCF optimization converged or not.
        mo_coeff : ndarray
            Optimized CASSCF orbitals coefficients

    Examples:

    >>> from pyscf import gto, scf, mcscf
    >>> mol = gto.M(atom='N 0 0 0; N 0 0 1', basis='ccpvdz', verbose=0)
    >>> mf = scf.RHF(mol)
    >>> mf.scf()
    >>> mc = mcscf.CASSCF(mf, 6, 6)
    >>> mc.kernel()[0]
    -109.044401882238134
    '''
    def __init__(self, mf, ncas, nelecas, ncore=None, frozen=None):
        casci.CASCI.__init__(self, mf, ncas, nelecas, ncore)
        self.frozen = frozen
# the max orbital rotation and CI increment, prefer small step size
        self.max_stepsize = .03
        self.max_cycle_macro = 50
        self.max_cycle_micro = 4
        self.max_cycle_micro_inner = 4
        self.conv_tol = 1e-7
        self.conv_tol_grad = None
        # for augmented hessian
        self.ah_level_shift = 1e-8
        self.ah_conv_tol = 1e-12
        self.ah_max_cycle = 30
        self.ah_lindep = 1e-14
# * ah_start_tol and ah_start_cycle control the start point to use AH step.
#   In function rotate_orb_cc, the orbital rotation is carried out with the
#   approximate aug_hessian step after a few davidson updates of the AH eigen
#   problem.  Reducing ah_start_tol or increasing ah_start_cycle will delay
#   the start point of orbital rotation.
# * We can do early ah_start since it only affect the first few iterations.
#   The start tol will be reduced when approach the convergence point.
# * Be careful with the SYMMETRY BROKEN caused by ah_start_tol/ah_start_cycle.
#   ah_start_tol/ah_start_cycle actually approximates the hessian to reduce
#   the J/K evaluation required by AH.  When the system symmetry is higher
#   than the one given by mol.symmetry/mol.groupname,  symmetry broken might
#   occur due to this approximation,  e.g.  with the default ah_start_tol,
#   C2 (16o, 8e) under D2h symmetry might break the degeneracy between
#   pi_x, pi_y orbitals since pi_x, pi_y belong to different irreps.  It can
#   be fixed by increasing the accuracy of AH solver, e.g.
#               ah_start_tol = 1e-8;  ah_conv_tol = 1e-10
        self.ah_start_tol = 2.5
        self.ah_start_cycle = 3
# * Classic AH can be simulated by setting eg
#               max_cycle_micro_inner = 1
#               ah_start_tol = 1e-7
#               max_stepsize = 1.5
#               ah_grad_trust_region = 1e6
# ah_grad_trust_region allow gradients increase for AH optimization
        self.ah_grad_trust_region = 3.0
        self.internal_rotation = False
        self.chkfile = mf.chkfile
        self.ci_response_space = 4
        self.ci_grad_trust_region = 3.0
        self.with_dep4 = False
        self.callback = None
        self.chk_ci = False

        self.fcisolver.max_cycle = 50

##################################################
# don't modify the following attributes, they are not input options
        self.e_tot = None
        self.e_cas = None
        self.ci = None
        self.mo_coeff = mf.mo_coeff
        self.mo_energy = mf.mo_energy
        self.converged = False
        self._max_stepsize = None

        self._keys = set(self.__dict__.keys())

    def dump_flags(self):
        log = logger.Logger(self.stdout, self.verbose)
        log.info('')
        log.info('******** %s flags ********', self.__class__)
        nvir = self.mo_coeff.shape[1] - self.ncore - self.ncas
        log.info('CAS (%de+%de, %do), ncore = %d, nvir = %d', \
                 self.nelecas[0], self.nelecas[1], self.ncas, self.ncore, nvir)
        if self.frozen is not None:
            log.info('frozen orbitals %s', str(self.frozen))
        log.info('max_cycle_macro = %d', self.max_cycle_macro)
        log.info('max_cycle_micro = %d', self.max_cycle_micro)
        log.info('conv_tol = %g', self.conv_tol)
        log.info('conv_tol_grad = %s', self.conv_tol_grad)
        log.info('max_cycle_micro_inner = %d', self.max_cycle_micro_inner)
        log.info('orbital rotation max_stepsize = %g', self.max_stepsize)
        log.info('augmented hessian ah_max_cycle = %d', self.ah_max_cycle)
        log.info('augmented hessian ah_conv_tol = %g', self.ah_conv_tol)
        log.info('augmented hessian ah_linear dependence = %g', self.ah_lindep)
        log.info('augmented hessian ah_level shift = %d', self.ah_level_shift)
        log.info('augmented hessian ah_start_tol = %g', self.ah_start_tol)
        log.info('augmented hessian ah_start_cycle = %d', self.ah_start_cycle)
        log.info('augmented hessian ah_grad_trust_region = %g', self.ah_grad_trust_region)
        log.info('ci_response_space = %d', self.ci_response_space)
        log.info('ci_grad_trust_region = %d', self.ci_grad_trust_region)
        log.info('with_dep4 %d', self.with_dep4)
        log.info('natorb = %s', self.natorb)
        log.info('canonicalization = %s', self.canonicalization)
        log.info('chkfile = %s', self.chkfile)
        log.info('max_memory %d MB (current use %d MB)',
                 self.max_memory, lib.current_memory()[0])
        log.info('internal_rotation = %s', self.internal_rotation)
        try:
            self.fcisolver.dump_flags(self.verbose)
        except AttributeError:
            pass
        if hasattr(self, 'max_orb_stepsize'):
            log.warn('Attribute "max_orb_stepsize" was replaced by "max_stepsize"')
        if self.mo_coeff is None:
            log.warn('Orbital for CASCI is not specified.  You probably need '
                     'call SCF.kernel() to initialize orbitals.')
        return self

    def kernel(self, mo_coeff=None, ci0=None, callback=None, _kern=kernel):
        if mo_coeff is None:
            mo_coeff = self.mo_coeff
        else: # overwrite self.mo_coeff because it is needed in many methods of this class
            self.mo_coeff = mo_coeff
        if callback is None: callback = self.callback

        if self.verbose >= logger.WARN:
            self.check_sanity()
        self.dump_flags()

        self.converged, self.e_tot, self.e_cas, self.ci, \
                self.mo_coeff, self.mo_energy = \
                _kern(self, mo_coeff,
                      tol=self.conv_tol, conv_tol_grad=self.conv_tol_grad,
                      ci0=ci0, callback=callback, verbose=self.verbose)
        logger.note(self, 'CASSCF energy = %.15g', self.e_tot)
        self._finalize()
        return self.e_tot, self.e_cas, self.ci, self.mo_coeff, self.mo_energy

    def mc1step(self, mo_coeff=None, ci0=None, callback=None):
        return self.kernel(mo_coeff, ci0, callback)

    def mc2step(self, mo_coeff=None, ci0=None, callback=None):
        from pyscf.mcscf import mc2step
        return self.kernel(mo_coeff, ci0, callback, mc2step.kernel)

    def casci(self, mo_coeff, ci0=None, eris=None, verbose=None, envs=None):
        if eris is None:
            fcasci = copy.copy(self)
            fcasci.ao2mo = self.get_h2cas
        else:
            fcasci = _fake_h_for_fast_casci(self, mo_coeff, eris)

        if isinstance(verbose, logger.Logger):
            log = verbose
        else:
            if verbose is None:
                verbose = self.verbose
            log = logger.Logger(self.stdout, verbose)

        e_tot, e_ci, fcivec = casci.kernel(fcasci, mo_coeff, ci0, log)
        if not isinstance(e_ci, (float, numpy.number)):
            raise RuntimeError('Multiple roots are detected in fcisolver.  '
                               'CASSCF does not know which state to optimize.\n'
                               'See also  mcscf.state_average  or  mcscf.state_specific  for excited states.')

        if envs is not None and log.verbose >= logger.INFO:
            log.debug('CAS space CI energy = %.15g', e_ci)

            if hasattr(self.fcisolver,'spin_square'):
                ss = self.fcisolver.spin_square(fcivec, self.ncas, self.nelecas)
            else:
                ss = None

            if 'imicro' in envs:  # Within CASSCF iteration
                if ss is None:
                    log.info('macro iter %d (%d JK  %d micro), '
                             'CASSCF E = %.15g  dE = %.8g',
                             envs['imacro'], envs['njk'], envs['imicro'],
                             e_tot, e_tot-envs['elast'])
                else:
                    log.info('macro iter %d (%d JK  %d micro), '
                             'CASSCF E = %.15g  dE = %.8g  S^2 = %.7f',
                             envs['imacro'], envs['njk'], envs['imicro'],
                             e_tot, e_tot-envs['elast'], ss[0])
                if 'norm_gci' in envs:
                    log.info('               |grad[o]|=%5.3g  '
                             '|grad[c]|= %s  |ddm|=%5.3g',
                             envs['norm_gorb0'],
                             envs['norm_gci'], envs['norm_ddm'])
                else:
                    log.info('               |grad[o]|=%5.3g  |ddm|=%5.3g',
                             envs['norm_gorb0'], envs['norm_ddm'])
            else:  # Initialization step
                if ss is None:
                    log.info('CASCI E = %.15g', e_tot)
                else:
                    log.info('CASCI E = %.15g  S^2 = %.7f', e_tot, ss[0])
        return e_tot, e_ci, fcivec

    def uniq_var_indices(self, nmo, ncore, ncas, frozen):
        nocc = ncore + ncas
        mask = numpy.zeros((nmo,nmo),dtype=bool)
        mask[ncore:nocc,:ncore] = True
        mask[nocc:,:nocc] = True
        if self.internal_rotation:
            mask[ncore:nocc,ncore:nocc][numpy.tril_indices(ncas,-1)] = True
        if frozen is not None:
            if isinstance(frozen, (int, numpy.integer)):
                mask[:frozen] = mask[:,:frozen] = False
            else:
                frozen = numpy.asarray(frozen)
                mask[frozen] = mask[:,frozen] = False
        return mask

    def pack_uniq_var(self, mat):
        nmo = self.mo_coeff.shape[1]
        idx = self.uniq_var_indices(nmo, self.ncore, self.ncas, self.frozen)
        return mat[idx]

    # to anti symmetric matrix
    def unpack_uniq_var(self, v):
        nmo = self.mo_coeff.shape[1]
        idx = self.uniq_var_indices(nmo, self.ncore, self.ncas, self.frozen)
        mat = numpy.zeros((nmo,nmo))
        mat[idx] = v
        return mat - mat.T

    def update_rotate_matrix(self, dx, u0=1):
        dr = self.unpack_uniq_var(dx)
        return numpy.dot(u0, expmat(dr))

    gen_g_hop = gen_g_hop
    rotate_orb_cc = rotate_orb_cc

    def update_ao2mo(self, mo):
        raise DeprecationWarning('update_ao2mo was obseleted since pyscf v1.0.  '
                                 'Use .ao2mo method instead')

    def ao2mo(self, mo):
#        nmo = mo.shape[1]
#        ncore = self.ncore
#        ncas = self.ncas
#        nocc = ncore + ncas
#        eri = pyscf.ao2mo.incore.full(self._scf._eri, mo)
#        eri = pyscf.ao2mo.restore(1, eri, nmo)
#        eris = lambda:None
#        eris.j_cp = numpy.einsum('iipp->ip', eri[:ncore,:ncore,:,:])
#        eris.k_cp = numpy.einsum('ippi->ip', eri[:ncore,:,:,:ncore])
#        eris.vhf_c =(numpy.einsum('iipq->pq', eri[:ncore,:ncore,:,:])*2
#                    -numpy.einsum('ipqi->pq', eri[:ncore,:,:,:ncore]))
#        eris.ppaa = numpy.asarray(eri[:,:,ncore:nocc,ncore:nocc], order='C')
#        eris.papa = numpy.asarray(eri[:,ncore:nocc,:,ncore:nocc], order='C')
#        return eris

        return mc_ao2mo._ERIS(self, mo, method='incore', level=2)

    def get_h2eff(self, mo_coeff=None):
        return self.get_h2cas(mo_coeff)
    def get_h2cas(self, mo_coeff=None):
        return casci.CASCI.ao2mo(self, mo_coeff)

    def update_jk_in_ah(self, mo, r, casdm1, eris):
# J3 = eri_popc * pc + eri_cppo * cp
# K3 = eri_ppco * pc + eri_pcpo * cp
# J4 = eri_pcpa * pa + eri_appc * ap
# K4 = eri_ppac * pa + eri_papc * ap
        ncore = self.ncore
        ncas = self.ncas
        nocc = ncore + ncas

        dm3 = reduce(numpy.dot, (mo[:,:ncore], r[:ncore,ncore:], mo[:,ncore:].T))
        dm3 = dm3 + dm3.T
        dm4 = reduce(numpy.dot, (mo[:,ncore:nocc], casdm1, r[ncore:nocc], mo.T))
        dm4 = dm4 + dm4.T
        vj, vk = self.get_jk(self.mol, (dm3,dm3*2+dm4))
        va = reduce(numpy.dot, (casdm1, mo[:,ncore:nocc].T, vj[0]*2-vk[0], mo))
        vc = reduce(numpy.dot, (mo[:,:ncore].T, vj[1]*2-vk[1], mo[:,ncore:]))
        return va, vc

# hessian_co exactly expands up to first order of H
# update_casdm exand to approx 2nd order of H
    def update_casdm(self, mo, u, fcivec, e_ci, eris, envs={}):
        nmo = mo.shape[1]
        rmat = u - numpy.eye(nmo)

        #g = hessian_co(self, mo, rmat, fcivec, e_ci, eris)
        ### hessian_co part start ###
        ncas = self.ncas
        nelecas = self.nelecas
        ncore = self.ncore
        nocc = ncore + ncas
        uc = u[:,:ncore]
        ua = u[:,ncore:nocc].copy()
        ra = rmat[:,ncore:nocc].copy()
        h1e_mo = reduce(numpy.dot, (mo.T, self.get_hcore(), mo))
        ddm = numpy.dot(uc, uc.T) * 2
        ddm[numpy.diag_indices(ncore)] -= 2
        if self.with_dep4:
            mo1 = numpy.dot(mo, u)
            mo1_cas = mo1[:,ncore:nocc]
            dm_core  = numpy.dot(mo1[:,:ncore], mo1[:,:ncore].T) * 2
            vj, vk = self._scf.get_jk(self.mol, dm_core)
            h1 =(reduce(numpy.dot, (ua.T, h1e_mo, ua)) +
                 reduce(numpy.dot, (mo1_cas.T, vj-vk*.5, mo1_cas)))
            eris._paaa = self._exact_paaa(mo, u)
            h2 = eris._paaa[ncore:nocc]
            vj = vk = None
        else:
            p1aa = numpy.empty((nmo,ncas,ncas**2))
            paa1 = numpy.empty((nmo,ncas**2,ncas))
            jk = reduce(numpy.dot, (ua.T, eris.vhf_c, ua))
            for i in range(nmo):
                jbuf = eris.ppaa[i]
                kbuf = eris.papa[i]
                jk +=(numpy.einsum('quv,q->uv', jbuf, ddm[i])
                    - numpy.einsum('uqv,q->uv', kbuf, ddm[i]) * .5)
                p1aa[i] = lib.dot(ua.T, jbuf.reshape(nmo,-1))
                paa1[i] = lib.dot(kbuf.transpose(0,2,1).reshape(-1,nmo), ra)
            h1 = reduce(numpy.dot, (ua.T, h1e_mo, ua)) + jk
            aa11 = lib.dot(ua.T, p1aa.reshape(nmo,-1)).reshape((ncas,)*4)
            aaaa = eris.ppaa[ncore:nocc,ncore:nocc,:,:]
            aa11 = aa11 + aa11.transpose(2,3,0,1) - aaaa

            a11a = numpy.dot(ra.T, paa1.reshape(nmo,-1)).reshape((ncas,)*4)
            a11a = a11a + a11a.transpose(1,0,2,3)
            a11a = a11a + a11a.transpose(0,1,3,2)
            h2 = aa11 + a11a
            jbuf = kbuf = p1aa = paa1 = aaaa = aa11 = a11a = None

        # pure core response
        # response of (1/2 dm * vhf * dm) ~ ddm*vhf
# Should I consider core response as a part of CI gradients?
        ecore =(numpy.einsum('pq,pq->', h1e_mo, ddm)
              + numpy.einsum('pq,pq->', eris.vhf_c, ddm))
        ### hessian_co part end ###

        ci1, g = self.solve_approx_ci(h1, h2, fcivec, ecore, e_ci, envs)
        if g is not None:  # So state average CI, DMRG etc will not be applied
            ovlp = numpy.dot(fcivec.ravel(), ci1.ravel())
            norm_g = numpy.linalg.norm(g)
            if 1-abs(ovlp) > norm_g * self.ci_grad_trust_region:
                logger.debug(self, '<ci1|ci0>=%5.3g |g|=%5.3g, ci1 out of trust region',
                             ovlp, norm_g)
                ci1 = fcivec.ravel() + g
                ci1 *= 1/numpy.linalg.norm(ci1)
        casdm1, casdm2 = self.fcisolver.make_rdm12(ci1, ncas, nelecas)

        return casdm1, casdm2, g, ci1

    def solve_approx_ci(self, h1, h2, ci0, ecore, e_ci, envs):
        ''' Solve CI eigenvalue/response problem approximately
        '''
        ncas = self.ncas
        nelecas = self.nelecas
        ncore = self.ncore
        nocc = ncore + ncas
        if 'norm_gorb' in envs:
            tol = max(self.conv_tol, envs['norm_gorb']**2*.1)
        else:
            tol = None
        if hasattr(self.fcisolver, 'approx_kernel'):
            fn = self.fcisolver.approx_kernel
            ci1 = fn(h1, h2, ncas, nelecas, ci0=ci0,
                     tol=tol, max_memory=self.max_memory)[1]
            return ci1, None
        elif not (hasattr(self.fcisolver, 'contract_2e') and
                  hasattr(self.fcisolver, 'absorb_h1e')):
            fn = self.fcisolver.kernel
            ci1 = fn(h1, h2, ncas, nelecas, ci0=ci0,
                     tol=tol, max_memory=self.max_memory,
                     max_cycle=self.ci_response_space)[1]
            return ci1, None

        h2eff = self.fcisolver.absorb_h1e(h1, h2, ncas, nelecas, .5)
        hc = self.fcisolver.contract_2e(h2eff, ci0, ncas, nelecas).ravel()

        g = hc - (e_ci-ecore) * ci0.ravel()
        if self.ci_response_space > 7:
            logger.debug(self, 'CI step by full response')
            # full response
            max_memory = max(400, self.max_memory-lib.current_memory()[0])
            e, ci1 = self.fcisolver.kernel(h1, h2, ncas, nelecas, ci0=ci0,
                                           tol=tol, max_memory=max_memory)
        else:
            nd = min(max(self.ci_response_space, 2), ci0.size)
            logger.debug(self, 'CI step by %dD subspace response', nd)
            xs = [ci0.ravel()]
            ax = [hc]
            heff = numpy.empty((nd,nd))
            seff = numpy.empty((nd,nd))
            heff[0,0] = numpy.dot(xs[0], ax[0])
            seff[0,0] = 1
            for i in range(1, nd):
                xs.append(ax[i-1] - xs[i-1] * e_ci)
                ax.append(self.fcisolver.contract_2e(h2eff, xs[i], ncas,
                                                     nelecas).ravel())
                for j in range(i+1):
                    heff[i,j] = heff[j,i] = numpy.dot(xs[i], ax[j])
                    seff[i,j] = seff[j,i] = numpy.dot(xs[i], xs[j])
            e, v = lib.safe_eigh(heff, seff)[:2]
            ci1 = xs[0] * v[0,0]
            for i in range(1,nd):
                ci1 += xs[i] * v[i,0]
        return ci1, g

    def get_jk(self, mol, dm, hermi=1):
        return self._scf.get_jk(mol, dm, hermi=1)

    def _exact_paaa(self, mo, u, out=None):
        nmo = mo.shape[1]
        ncore = self.ncore
        ncas = self.ncas
        nocc = ncore + ncas
        mo1 = numpy.dot(mo, u)
        mo1_cas = mo1[:,ncore:nocc]
        mos = (mo1_cas, mo1_cas, mo1, mo1_cas)
        if self._scf._eri is None:
            aapa = ao2mo.general(self.mol, mos)
        else:
            aapa = ao2mo.general(self._scf._eri, mos)
        paaa = numpy.empty((nmo*ncas,ncas*ncas))
        buf = numpy.empty((ncas,ncas,nmo*ncas))
        for ij, (i, j) in enumerate(zip(*numpy.tril_indices(ncas))):
            buf[i,j] = buf[j,i] = aapa[ij]
        paaa = lib.transpose(buf.reshape(ncas*ncas,-1), out=out)
        return paaa.reshape(nmo,ncas,ncas,ncas)

    def dump_chk(self, envs):
        if hasattr(self.fcisolver, 'nevpt_intermediate'):
            civec = None
        elif self.chk_ci:
            civec = envs['fcivec']
        else:
            civec = None
        ncore = self.ncore
        nocc = self.ncore + self.ncas
        mo_occ = numpy.zeros(envs['mo'].shape[1])
        mo_occ[:ncore] = 2
        if self.natorb:
            occ = self._eig(-envs['casdm1'], ncore, nocc)[0]
            mo_occ[ncore:nocc] = -occ
        else:
            mo_occ[ncore:nocc] = envs['casdm1'].diagonal()
# Note: mo_energy in active space =/= F_{ii}  (F is general Fock)
        if 'mo_energy' in envs:
            mo_energy = envs['mo_energy']
        else:
            mo_energy = 'None'
        chkfile.dump_mcscf(self, self.chkfile, 'mcscf', envs['e_tot'],
                           envs['mo'], self.ncore, self.ncas, mo_occ,
                           mo_energy, envs['e_ci'], civec, envs['casdm1'],
                           overwrite_mol=False)
        return self

    def update(self, chkfile=None):
        return self.update_from_chk(chkfile)
    def update_from_chk(self, chkfile=None):
        if chkfile is None: chkfile = self.chkfile
        self.__dict__.update(lib.chkfile.load(chkfile, 'mcscf'))
        return self

    def rotate_mo(self, mo, u, log=None):
        '''Rotate orbitals with the given unitary matrix'''
        mo = numpy.dot(mo, u)
        if log is not None and log.verbose >= logger.DEBUG:
            ncore = self.ncore
            ncas = self.ncas
            nocc = ncore + ncas
            s = reduce(numpy.dot, (mo[:,ncore:nocc].T, self._scf.get_ovlp(),
                                   self.mo_coeff[:,ncore:nocc]))
            log.debug('Active space overlap to initial guess, SVD = %s',
                      numpy.linalg.svd(s)[1])
            log.debug('Active space overlap to last step, SVD = %s',
                      numpy.linalg.svd(u[ncore:nocc,ncore:nocc])[1])
        return mo

    def micro_cycle_scheduler(self, envs):
        #log_norm_ddm = numpy.log(envs['norm_ddm'])
        #return max(self.max_cycle_micro, int(self.max_cycle_micro-1-log_norm_ddm))
        return self.max_cycle_micro

    def max_stepsize_scheduler(self, envs):
        if self._max_stepsize is None:
            self._max_stepsize = self.max_stepsize
        if envs['de'] > self.conv_tol:  # Avoid total energy increasing
            self._max_stepsize *= .5
            logger.debug(self, 'set max_stepsize to %g', self._max_stepsize)
        else:
            self._max_stepsize = numpy.sqrt(self.max_stepsize*self.max_stepsize)
        return self._max_stepsize

    def ah_scheduler(self, envs):
        pass

    @property
    def ci_update_dep(self):
        return self.with_dep4
    @ci_update_dep.setter
    def ci_update_dep(self, x):
        sys.stderr.write('WARN: Attribute .ci_update_dep was replaced by .with_dep4 since PySCF v1.1.\n')
        self.with_dep4 = x == 4
    grad_update_dep = ci_update_dep


# to avoid calculating AO integrals
def _fake_h_for_fast_casci(casscf, mo, eris):
    mc = copy.copy(casscf)
    mc.mo_coeff = mo
    # vhf for core density matrix
    mo_inv = numpy.dot(mo.T, mc._scf.get_ovlp())
    vhf = reduce(numpy.dot, (mo_inv.T, eris.vhf_c, mo_inv))
    mc.get_veff = lambda *args: vhf

    ncore = casscf.ncore
    nocc = ncore + casscf.ncas
    eri_cas = eris.ppaa[ncore:nocc,ncore:nocc,:,:].copy()
    mc.get_h2eff = lambda *args: eri_cas
    return mc

def expmat(a):
    return scipy.linalg.expm(a)


if __name__ == '__main__':
    from pyscf import gto
    from pyscf import scf
    import pyscf.fci
    from pyscf.mcscf import addons

    mol = gto.Mole()
    mol.verbose = 0
    mol.output = None#"out_h2o"
    mol.atom = [
        ['H', ( 1.,-1.    , 0.   )],
        ['H', ( 0.,-1.    ,-1.   )],
        ['H', ( 1.,-0.5   ,-1.   )],
        ['H', ( 0.,-0.5   ,-1.   )],
        ['H', ( 0.,-0.5   ,-0.   )],
        ['H', ( 0.,-0.    ,-1.   )],
        ['H', ( 1.,-0.5   , 0.   )],
        ['H', ( 0., 1.    , 1.   )],
    ]

    mol.basis = {'H': 'sto-3g',
                 'O': '6-31g',}
    mol.build()

    m = scf.RHF(mol)
    ehf = m.scf()
    emc = kernel(CASSCF(m, 4, 4), m.mo_coeff, verbose=4)[1]
    print(ehf, emc, emc-ehf)
    print(emc - -3.22013929407)

    mc = CASSCF(m, 4, (3,1))
    mc.verbose = 4
    #mc.fcisolver = pyscf.fci.direct_spin1
    mc.fcisolver = pyscf.fci.solver(mol, False)
    emc = kernel(mc, m.mo_coeff, verbose=4)[1]
    print(emc - -15.950852049859-mol.energy_nuc())


    mol.atom = [
        ['O', ( 0., 0.    , 0.   )],
        ['H', ( 0., -0.757, 0.587)],
        ['H', ( 0., 0.757 , 0.587)],]
    mol.basis = {'H': 'cc-pvdz',
                 'O': 'cc-pvdz',}
    mol.build()

    m = scf.RHF(mol)
    ehf = m.scf()
    mc = CASSCF(m, 6, 4)
    mc.fcisolver = pyscf.fci.solver(mol)
    mc.verbose = 4
    mo = addons.sort_mo(mc, m.mo_coeff, (3,4,6,7,8,9), 1)
    emc = mc.mc1step(mo)[0]
    print(ehf, emc, emc-ehf)
    #-76.0267656731 -76.0873922924 -0.0606266193028
    print(emc - -76.0873923174, emc - -76.0926176464)

    mc = CASSCF(m, 6, (3,1))
    mo = addons.sort_mo(mc, m.mo_coeff, (3,4,6,7,8,9), 1)
    #mc.fcisolver = pyscf.fci.direct_spin1
    mc.fcisolver = pyscf.fci.solver(mol, False)
    mc.verbose = 4
    emc = mc.mc1step(mo)[0]
    #mc.analyze()
    print(emc - -75.7155632535814)

    mc.internal_rotation = True
    emc = mc.mc1step(mo)[0]
    print(emc - -75.7155632535814)
