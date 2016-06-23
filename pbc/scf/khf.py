'''
Hartree-Fock for periodic systems with k-point sampling

See Also:
    hf.py : Hartree-Fock for periodic systems at a single k-point
'''

import time
import numpy as np
import scipy.special
import h5py
import pyscf.scf.hf
from pyscf.pbc.scf import hf as pbchf
from pyscf import lib
from pyscf.lib import logger
from pyscf.pbc import tools
from pyscf.pbc.scf import addons


def get_ovlp(mf, cell=None, kpts=None):
    '''Get the overlap AO matrices at sampled k-points.

    Args:
        kpts : (nkpts, 3) ndarray

    Returns:
        ovlp_kpts : (nkpts, nao, nao) ndarray
    '''
    if cell is None: cell = mf.cell
    if kpts is None: kpts = mf.kpts
    return lib.asarray(cell.pbc_intor('cint1e_ovlp_sph', hermi=1, kpts=kpts))


def get_hcore(mf, cell=None, kpts=None):
    '''Get the core Hamiltonian AO matrices at sampled k-points.

    Args:
        kpts : (nkpts, 3) ndarray

    Returns:
        hcore : (nkpts, nao, nao) ndarray
    '''
    if cell is None: cell = mf.cell
    if kpts is None: kpts = mf.kpts
    return lib.asarray([pbchf.get_hcore(cell, k) for k in kpts])


def get_j(mf, cell, dm_kpts, kpts, kpt_band=None):
    '''Get the Coulomb (J) AO matrix at sampled k-points.

    Args:
        dm_kpts : (nkpts, nao, nao) ndarray or a list of (nkpts,nao,nao) ndarray
            Density matrix at each k-point.  If a list of k-point DMs, eg,
            UHF alpha and beta DM, the alpha and beta DMs are contracted
            separately.
        kpts : (nkpts, 3) ndarray

    Kwargs:
        kpt_band : (3,) ndarray
            An arbitrary "band" k-point at which to evalute the matrix.

    Returns:
        vj : (nkpts, nao, nao) ndarray
        or list of vj if the input dm_kpts is a list of DMs
    '''
    from pyscf.pbc.dft import gen_grid
    from pyscf.pbc.dft import numint
    coords = gen_grid.gen_uniform_grids(cell)
    nkpts = len(kpts)
    ngs = len(coords)
    dm_kpts = np.asarray(dm_kpts)
    nao = dm_kpts.shape[-1]

    ni = numint._KNumInt(kpts)
    aoR_kpts = ni.eval_ao(cell, coords, kpts)
    if kpt_band is not None:
        aoR_kband = numint.eval_ao(cell, coords, kpt_band)

    dms = dm_kpts.reshape(-1,nkpts,nao,nao)
    nset = dms.shape[0]

    vjR = [get_vjR(cell, dms[i], aoR_kpts) for i in range(nset)]
    if kpt_band is not None:
        vj_kpts = [cell.vol/ngs * lib.dot(aoR_kband.T.conj()*vjR[i], aoR_kband)
                   for i in range(nset)]
        if dm_kpts.ndim == 3:  # One set of dm_kpts for KRHF
            vj_kpts = vj_kpts[0]
        return lib.asarray(vj_kpts)
    else:
        vj_kpts = []
        for i in range(nset):
            vj = [cell.vol/ngs * lib.dot(aoR_k.T.conj()*vjR[i], aoR_k)
                  for aoR_k in aoR_kpts]
            vj_kpts.append(lib.asarray(vj))
        return lib.asarray(vj_kpts).reshape(dm_kpts.shape)


def get_jk(mf, cell, dm_kpts, kpts, kpt_band=None):
    '''Get the Coulomb (J) and exchange (K) AO matrices at sampled k-points.

    Args:
        dm_kpts : (nkpts, nao, nao) ndarray
            Density matrix at each k-point
        kpts : (nkpts, 3) ndarray

    Kwargs:
        kpt_band : (3,) ndarray
            An arbitrary "band" k-point at which to evalute the matrix.

    Returns:
        vj : (nkpts, nao, nao) ndarray
        vk : (nkpts, nao, nao) ndarray
        or list of vj and vk if the input dm_kpts is a list of DMs
    '''
    from pyscf.pbc.dft import gen_grid
    from pyscf.pbc.dft import numint
    coords = gen_grid.gen_uniform_grids(cell)
    nkpts = len(kpts)
    ngs = len(coords)
    dm_kpts = np.asarray(dm_kpts)
    nao = dm_kpts.shape[-1]

    dms = dm_kpts.reshape(-1,nkpts,nao,nao)
    nset = dms.shape[0]

    ni = numint._KNumInt(kpts)
    aoR_kpts = ni.eval_ao(cell, coords, kpts)
    if kpt_band is not None:
        aoR_kband = numint.eval_ao(cell, coords, kpt_band)

# J
    vjR = [get_vjR(cell, dms[i], aoR_kpts) for i in range(nset)]
    if kpt_band is not None:
        vj_kpts = [cell.vol/ngs * lib.dot(aoR_kband.T.conj()*vjR[i], aoR_kband)
                   for i in range(nset)]
    else:
        vj_kpts = []
        for i in range(nset):
            vj = [cell.vol/ngs * lib.dot(aoR_k.T.conj()*vjR[i], aoR_k)
                  for aoR_k in aoR_kpts]
            vj_kpts.append(lib.asarray(vj))
    vj_kpts = lib.asarray(vj_kpts)
    vjR = None

# K
    weight = 1./nkpts * (cell.vol/ngs)
    vk_kpts = np.zeros_like(vj_kpts)
    if kpt_band is not None:
        for k2, kpt2 in enumerate(kpts):
            aoR_dms = [lib.dot(aoR_kpts[k2], dms[i,k2]) for i in range(nset)]
            vkR_k1k2 = pbchf.get_vkR(mf, cell, aoR_kband, aoR_kpts[k2],
                                     kpt_band, kpt2)
            #:vk_kpts = 1./nkpts * (cell.vol/ngs) * np.einsum('rs,Rp,Rqs,Rr->pq',
            #:            dm_kpts[k2], aoR_kband.conj(),
            #:            vkR_k1k2, aoR_kpts[k2])
            for i in range(nset):
                tmp_Rq = np.einsum('Rqs,Rs->Rq', vkR_k1k2, aoR_dms[i])
                vk_kpts[i] += weight * lib.dot(aoR_kband.T.conj(), tmp_Rq)
            vkR_k1k2 = None
        if dm_kpts.ndim == 3:
            vj_kpts = vj_kpts[0]
            vk_kpts = vk_kpts[0]
        return lib.asarray(vj_kpts), lib.asarray(vk_kpts)
    else:
        for k2, kpt2 in enumerate(kpts):
            aoR_dms = [lib.dot(aoR_kpts[k2], dms[i,k2]) for i in range(nset)]
            for k1, kpt1 in enumerate(kpts):
                vkR_k1k2 = pbchf.get_vkR(mf, cell, aoR_kpts[k1], aoR_kpts[k2],
                                         kpt1, kpt2)
                for i in range(nset):
                    tmp_Rq = np.einsum('Rqs,Rs->Rq', vkR_k1k2, aoR_dms[i])
                    vk_kpts[i,k1] += weight * lib.dot(aoR_kpts[k1].T.conj(), tmp_Rq)
            vkR_k1k2 = None
        return vj_kpts.reshape(dm_kpts.shape), vk_kpts.reshape(dm_kpts.shape)


def get_vjR(cell, dm_kpts, aoR_kpts):
    '''Get the real-space Hartree potential of the k-point sampled density matrix.

    Returns:
        vR : (ngs,) ndarray
            The real-space Hartree potential at every grid point.
    '''
    from pyscf.pbc.dft import numint
    nkpts = len(aoR_kpts)
    coulG = tools.get_coulG(cell)

    rhoR = 0
    for k in range(nkpts):
        rhoR += 1./nkpts*numint.eval_rho(cell, aoR_kpts[k], dm_kpts[k])
    rhoG = tools.fft(rhoR, cell.gs)

    vG = coulG*rhoG
    vR = tools.ifft(vG, cell.gs)
    if rhoR.dtype == np.double:
        vR = vR.real
    return vR


def get_occ(mf, mo_energy_kpts=None, mo_coeff_kpts=None):
    '''Label the occupancies for each orbital for sampled k-points.

    This is a k-point version of scf.hf.SCF.get_occ
    '''
    if mo_energy_kpts is None: mo_energy_kpts = mf.mo_energy
    mo_occ_kpts = np.zeros_like(mo_energy_kpts)

    nkpts = mo_energy_kpts.shape[0]
    nocc = (mf.cell.nelectron * nkpts) // 2

    # TODO: implement Fermi smearing and print mo_energy kpt by kpt
    mo_energy = np.sort(mo_energy_kpts.ravel())
    fermi = mo_energy[nocc-1]
    mo_occ_kpts[mo_energy_kpts <= fermi] = 2

    if nocc < mo_energy.size:
        logger.info(mf, 'HOMO = %.12g  LUMO = %.12g',
                    mo_energy[nocc-1], mo_energy[nocc])
        if mo_energy[nocc-1]+1e-3 > mo_energy[nocc]:
            logger.warn(mf, '!! HOMO %.12g == LUMO %.12g',
                        mo_energy[nocc-1], mo_energy[nocc])
    else:
        logger.info(mf, 'HOMO = %.12g', mo_energy[nocc-1])

    if mf.verbose >= logger.DEBUG:
        np.set_printoptions(threshold=len(mo_energy))
        logger.debug(mf, '     k-point                  mo_energy')
        for k,kpt in enumerate(mf.kpts):
            kstr = '(%6.3f %6.3f %6.3f)' % tuple(mf.cell.get_scaled_kpts(kpt))
            logger.debug(mf, '  %2d %s   %s %s', k, kstr,
                         mo_energy_kpts[k,mo_occ_kpts[k]> 0],
                         mo_energy_kpts[k,mo_occ_kpts[k]==0])
        np.set_printoptions()

    return mo_occ_kpts


def make_rdm1(mo_coeff_kpts, mo_occ_kpts):
    '''One particle density matrices for all k-points.

    Returns:
        dm_kpts : (nkpts, nao, nao) ndarray
    '''
    nkpts = len(mo_occ_kpts)
    dm_kpts = [pyscf.scf.hf.make_rdm1(mo_coeff_kpts[k], mo_occ_kpts[k])
               for k in range(nkpts)]
    return lib.asarray(dm_kpts)


def energy_elec(mf, dm_kpts=None, h1e_kpts=None, vhf_kpts=None):
    '''Following pyscf.scf.hf.energy_elec()
    '''
    if dm_kpts is None: dm_kpts = mf.make_rdm1()
    if h1e_kpts is None: h1e_kpts = mf.get_hcore()
    if vhf_kpts is None: vhf_kpts = mf.get_veff(mf.cell, dm_kpts)

    nkpts = len(dm_kpts)
    e1 = 1./nkpts * np.einsum('kij,kji', dm_kpts, h1e_kpts)
    e_coul = 1./nkpts * np.einsum('kij,kji', dm_kpts, vhf_kpts) * 0.5
    if abs(e_coul.imag > 1.e-10):
        raise RuntimeError("Coulomb energy has imaginary part, "
                           "something is wrong!", e_coul.imag)
    e1 = e1.real
    e_coul = e_coul.real
    logger.debug(mf, 'E_coul = %.15g', e_coul)
    return e1+e_coul, e_coul


def init_guess_by_chkfile(cell, chkfile_name, project=True, kpts=None):
    '''Read the KHF results from checkpoint file, then project it to the
    basis defined by ``cell``

    Returns:
        Density matrix, 3D ndarray
    '''
    chk_cell, scf_rec = pyscf.pbc.scf.chkfile.load_scf(chkfile_name)

    if kpts is None:
        kpts = scf_rec['kpts']

    if 'kpt' in scf_rec:
        chk_kpts = scf_rec['kpt'].reshape(-1,3)
    elif 'kpts' in scf_rec:
        chk_kpts = scf_rec['kpts']
    else:
        chk_kpts = np.zeros((1,3))

    mo = scf_rec['mo_coeff']
    mo_occ = scf_rec['mo_occ']
    if 'kpts' not in scf_rec:  # gamma point or single k-point
        if mo.ndim == 2:
            mo = mo.reshape((1,)+mo.shape)
            mo_occ = mo_occ.reshape((1,)+mo_occ.shape)
        else:  # UHF
            mo = mo.reshape((2,1)+mo.shape[1:])
            mo_occ = mo_occ.reshape((2,1)+mo_occ.shape[1:])

    def fproj(mo, kpt):
        if project:
            return addons.project_mo_nr2nr(chk_cell, mo, cell, kpt)
        else:
            return mo

    if kpts.shape == chk_kpts.shape and np.allclose(kpts, chk_kpts):
        def makedm(mos, occs):
            mos = [fproj(mo, None) for mo in mos]
            return make_rdm1(mos, occs)
    else:
        where = [np.argmin(lib.norm(chk_kpts-kpt, axis=1)) for kpt in kpts]
        def makedm(mos, occs):
            mos = [fproj(mos[w], chk_kpts[w]-kpts[i]) for i,w in enumerate(where)]
            return make_rdm1(mos, occs[where])

    if mo.ndim == 3:  # KRHF
        dm = makedm(mo, mo_occ)
    else:  # KUHF
        dm = makedm(mo[0], mo_occ[0]) + makedm(mo[1], mo_occ[1])

    # Real DM for gamma point
    if np.allclose(kpts, 0):
        dm = dm.real
    return dm


class KRHF(pyscf.scf.hf.RHF):
    '''RHF class with k-point sampling.

    Compared to molecular SCF, some members such as mo_coeff, mo_occ
    now have an additional first dimension for the k-points,
    e.g. mo_coeff is (nkpts, nao, nao) ndarray

    Attributes:
        kpts : (nks,3) ndarray
            The sampling k-points in Cartesian coordinates, in units of 1/Bohr.
    '''
    def __init__(self, cell, kpts=np.zeros((1,3)), exxdiv='ewald'):
        from pyscf.pbc.df import PWDF
        if not cell._built:
            sys.stderr.write('Warning: cell.build() is not called in input\n')
            cell.build()
        self.cell = cell
        pyscf.scf.hf.RHF.__init__(self, cell)

        self.exxdiv = exxdiv
        self.with_df = PWDF(cell)
        self.kpts = kpts
        self.direct_scf = False

        self.exx_built = False
        self._keys = self._keys.union(['cell', 'exx_built', 'exxdiv', 'with_df'])

    @property
    def kpts(self):
        return self.with_df.kpts
    @kpts.setter
    def kpts(self, x):
        self.with_df.kpts = np.reshape(x, (-1,3))

    @property
    def mo_energy_kpts(self):
        return self.mo_energy

    @property
    def mo_coeff_kpts(self):
        return self.mo_coeff

    @property
    def mo_occ_kpts(self):
        return self.mo_occ

    def dump_flags(self):
        pyscf.scf.hf.RHF.dump_flags(self)
        logger.info(self, '\n')
        logger.info(self, '******** PBC SCF flags ********')
        logger.info(self, 'N kpts = %d', len(self.kpts))
        logger.debug(self, 'kpts = %s', self.kpts)
        logger.info(self, 'DF object = %s', self.with_df)
        logger.info(self, 'Exchange divergence treatment (exxdiv) = %s', self.exxdiv)
        if self.exxdiv == 'vcut_ws':
            if self.exx_built is False:
                self.precompute_exx()
            logger.info(self, 'WS alpha = %s', self.exx_alpha)

    def build(self, cell=None):
        pyscf.scf.hf.RHF.build(self, cell)
        if self.exxdiv == 'vcut_ws':
            self.precompute_exx()

    def precompute_exx(self):
        from pyscf.pbc import gto as pbcgto
        from pyscf.pbc.dft import gen_grid
        log = logger.Logger(self.stdout, self.verbose)
        log.info("# Precomputing Wigner-Seitz EXX kernel")
        Nk = tools.get_monkhorst_pack_size(self.cell, self.kpts)
        log.info("# Nk = %d", Nk)
        kcell = pbcgto.Cell()
        kcell.atom = 'H 0. 0. 0.'
        kcell.spin = 1
        kcell.unit = 'B'
        kcell.h = kcell._h = self.cell._h * Nk
        Lc = 1.0/np.linalg.norm(np.linalg.inv(kcell.h.T), axis=0)
        log.info("# Lc = %d", Lc)
        Rin = Lc.min() / 2.0
        log.info("# Rin = %d", Rin)
        # ASE:
        alpha = 5./Rin # sqrt(-ln eps) / Rc, eps ~ 10^{-11}
        kcell.gs = np.array([2*int(L*alpha*3.0) for L in Lc])
        # QE:
        #alpha = 3./Rin * np.sqrt(0.5)
        #kcell.gs = (4*alpha*np.linalg.norm(kcell.h,axis=0)).astype(int)
        log.info("# kcell.gs FFT = %d", kcell.gs)
        kcell.build(False,False)
        vR = tools.ifft( tools.get_coulG(kcell), kcell.gs )
        kngs = len(vR)
        log.info("# kcell kngs = %d", kngs)
        rs = gen_grid.gen_uniform_grids(kcell)
        corners = np.dot(np.indices((2,2,2)).reshape((3,8)).T, kcell.h.T)
        for i, rv in enumerate(rs):
            # Minimum image convention to corners of kcell parallelepiped
            r = np.linalg.norm(rv-corners, axis=1).min()
            if np.isclose(r, 0.):
                vR[i] = 2*alpha / np.sqrt(np.pi)
            else:
                vR[i] = scipy.special.erf(alpha*r) / r
        vG = (kcell.vol/kngs) * tools.fft(vR, kcell.gs)
        self.exx_alpha = alpha
        self.exx_kcell = kcell
        self.exx_q = kcell.Gv
        self.exx_vq = vG
        self.exx_built = True
        log.info("# Finished precomputing")

    def get_init_guess(self, cell=None, key='minao'):
        if cell is None: cell = self.cell
        dm = pyscf.scf.hf.RHF.get_init_guess(self, cell, key)
        if key.lower() == 'chkfile':
            dm_kpts = dm
        else:
            dm_kpts = lib.asarray([dm]*len(self.kpts))
        return dm_kpts

    def get_hcore(self, cell=None, kpts=None):
        if cell is None: cell = self.cell
        if kpts is None: kpts = self.kpts
        if cell.pseudo is None:
            nuc = self.with_df.get_nuc(cell, kpts)
            t = cell.pbc_intor('cint1e_kin_sph', 1, 1, kpts)
            return [nuc[k] + t[k] for k, kpt in enumerate(self.kpts)]
        else:
            return get_hcore(self, cell, kpts)

    get_ovlp = get_ovlp
    get_occ = get_occ
    energy_elec = energy_elec

    def get_j(self, cell=None, dm_kpts=None, hermi=1, kpts=None, kpt_band=None):
        if cell is None: cell = self.cell
        if kpts is None: kpts = self.kpts
        if dm_kpts is None: dm_kpts = self.make_rdm1()
        cpu0 = (time.clock(), time.time())
        vj = self.with_df.get_jk(cell, dm_kpts, hermi, kpts, kpt_band,
                                 with_k=False)[0]
        logger.timer(self, 'vj', *cpu0)
        return vj

    def get_k(self, cell=None, dm_kpts=None, hermi=1, kpts=None, kpt_band=None):
        return self.get_jk(cell, dm_kpts, hermi, kpts, kpt_band)[1]

    def get_jk(self, cell=None, dm_kpts=None, hermi=1, kpts=None, kpt_band=None):
        if cell is None: cell = self.cell
        if kpts is None: kpts = self.kpts
        if dm_kpts is None: dm_kpts = self.make_rdm1()
        cpu0 = (time.clock(), time.time())
        vj, vk = self.with_df.get_jk(cell, dm_kpts, hermi, kpts, kpt_band, mf=self)
        logger.timer(self, 'vj and vk', *cpu0)
        return vj, vk

    def get_veff(self, cell=None, dm_kpts=None, dm_last=0, vhf_last=0, hermi=1,
                 kpts=None, kpt_band=None):
        '''Hartree-Fock potential matrix for the given density matrix.
        See :func:`scf.hf.get_veff` and :func:`scf.hf.RHF.get_veff`
        '''
        vj, vk = self.get_jk(cell, dm_kpts, hermi, kpts, kpt_band)
        return vj - vk * .5

    def get_grad(self, mo_coeff_kpts, mo_occ_kpts, fock=None):
        '''
        returns 1D array of gradients, like non K-pt version
        note that occ and virt indices of different k pts now occur
        in sequential patches of the 1D array
        '''
        if fock is None:
            dm1 = self.make_rdm1(mo_coeff_kpts, mo_occ_kpts)
            fock = self.get_hcore(self.cell, self.kpts) + self.get_veff(self.cell, dm1)

        nkpts = len(self.kpts)
        grad_kpts = [pyscf.scf.hf.get_grad(mo_coeff_kpts[k], mo_occ_kpts[k], fock[k])
                     for k in range(nkpts)]
        return np.hstack(grad_kpts)

    def eig(self, h_kpts, s_kpts):
        nkpts = len(h_kpts)
        eig_kpts = []
        mo_coeff_kpts = []

        for k in range(nkpts):
            e, c = pyscf.scf.hf.RHF.eig(self, h_kpts[k], s_kpts[k])
            eig_kpts.append(e)
            mo_coeff_kpts.append(c)
        return lib.asarray(eig_kpts), lib.asarray(mo_coeff_kpts)

    def make_rdm1(self, mo_coeff_kpts=None, mo_occ_kpts=None):
        if mo_coeff_kpts is None:
            # Note: this is actually "self.mo_coeff_kpts"
            # which is stored in self.mo_coeff of the scf.hf.RHF superclass
            mo_coeff_kpts = self.mo_coeff
        if mo_occ_kpts is None:
            # Note: this is actually "self.mo_occ_kpts"
            # which is stored in self.mo_occ of the scf.hf.RHF superclass
            mo_occ_kpts = self.mo_occ

        return make_rdm1(mo_coeff_kpts, mo_occ_kpts)

    def get_bands(self, kpt_band, cell=None, dm_kpts=None, kpts=None):
        '''Get energy bands at a given (arbitrary) 'band' k-point.

        Returns:
            mo_energy : (nao,) ndarray
                Bands energies E_n(k)
            mo_coeff : (nao, nao) ndarray
                Band orbitals psi_n(k)
        '''
        if cell is None: cell = self.cell
        if dm_kpts is None: dm_kpts = self.make_rdm1()
        if kpts is None: kpts = self.kpts

        fock = self.get_hcore(cell, kpt_band)
        fock = fock + self.get_veff(cell, dm_kpts, kpts=kpts, kpt_band=kpt_band)
        s1e = self.get_ovlp(cell, kpt_band)
        mo_energy, mo_coeff = pyscf.scf.hf.eig(fock, s1e)
        return mo_energy, mo_coeff

    def init_guess_by_chkfile(self, chk=None, project=True, kpts=None):
        if chk is None: chk = self.chkfile
        if kpts is None: kpts = self.kpts
        return init_guess_by_chkfile(self.cell, chk, project, kpts)
    def from_chk(self, chk=None, project=True, kpts=None):
        return self.init_guess_by_chkfile(chk, project, kpts)

    def dump_chk(self, envs):
        pyscf.scf.hf.RHF.dump_chk(self, envs)
        if self.chkfile:
            with h5py.File(self.chkfile) as fh5:
                fh5['scf/kpts'] = self.kpts
        return self
