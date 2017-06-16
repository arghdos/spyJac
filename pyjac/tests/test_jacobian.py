from . import TestClass
from ..core.rate_subs import (
    get_sri_kernel, assign_rates, get_concentrations,
    get_rop, get_rop_net, get_spec_rates, get_molar_rates, get_thd_body_concs,
    get_rxn_pres_mod)
from ..loopy_utils.loopy_utils import (auto_run, loopy_options,
                                       RateSpecialization,
                                       get_device_list,
                                       kernel_call,
                                       set_adept_editor,
                                       populate
                                       )
from ..core.create_jacobian import (
    dRopi_dnj, dci_thd_dnj)
from ..core import array_creator as arc
from ..kernel_utils import kernel_gen as k_gen

import numpy as np
import six
import loopy as lp
import cantera as ct

from nose.plugins.attrib import attr
from optionloop import OptionLoop

from collections import OrderedDict


class editor(object):

    def __init__(self, independent, dependent,
                 problem_size, order, do_not_set=[]):

        self.independent = independent
        indep_size = next(x for x in independent.shape if x != problem_size)
        self.dependent = dependent
        dep_size = next(x for x in dependent.shape if x != problem_size)
        self.problem_size = problem_size

        # create the jacobian
        self.output = arc.creator('jac', np.float64,
                                  (problem_size, dep_size, indep_size),
                                  order=order)
        self.output = self.output(*['i', 'j', 'k'])[0]
        try:
            self.do_not_set = do_not_set[:]
        except:
            self.do_not_set = [do_not_set]

    def set_single_kernel(self, single_kernel):
        """
        It's far easier to use two generated kernels, one that uses the full
        problem size (for calling via loopy), and another that uses a problem
        size of 1, to work with Adept indexing in the AD kernel
        """
        self.single_kernel = single_kernel

    def __call__(self, knl):
        return set_adept_editor(knl, self.single_kernel, self.problem_size,
                                self.independent, self.dependent, self.output,
                                self.do_not_set)


class SubTest(TestClass):

    def __get_eqs_and_oploop(self, do_ratespec=False, do_ropsplit=None,
                             do_spec_per_reac=False,
                             use_platform_instead=True,
                             do_conp=True,
                             do_vector=True,
                             langs=['opencl']):
        eqs = {'conp': self.store.conp_eqs,
               'conv': self.store.conv_eqs}
        vectypes = [4, None] if do_vector else [None]
        oploop = [('lang', langs),
                  ('width', vectypes[:]),
                  ('depth', vectypes[:]),
                  ('order', ['C', 'F']),
                  ('ilp', [False]),
                  ('unr', [None, 4]),
                  ('auto_diff', [True])
                  ]
        if do_ratespec:
            oploop += [
                ('rate_spec', [x for x in RateSpecialization]),
                ('rate_spec_kernels', [True, False])]
        if do_ropsplit:
            oploop += [
                ('rop_net_kernels', [True])]
        if do_spec_per_reac:
            oploop += [
                ('spec_rates_sum_over_reac', [True, False])]
        if use_platform_instead:
            oploop += [('platform', ['CPU'])]
        else:
            oploop += [('device', get_device_list())]
        if do_conp:
            oploop += [('conp', [True, False])]
        oploop += [('knl_type', ['map'])]
        oploop = OptionLoop(OrderedDict(oploop))

        return eqs, oploop

    def _generic_jac_tester(self, func, kernel_calls, do_ratespec=False,
                            do_ropsplit=None, do_spec_per_reac=False,
                            **kw_args):
        """
        A generic testing method that can be used for rate constants,
        third bodies, ...

        Parameters
        ----------
        func : function
            The function to test
        kernel_calls : :class:`kernel_call` or list thereof
            Contains the masks and reference answers for kernel testing
        do_ratespec : bool
            If true, test rate specializations and kernel splitting for simple
            rates
        do_ropsplit : bool
            If true, test kernel splitting for rop_net
        do_spec_per_reac : bool
            If true, test species rates summing over reactions as well
        """

        eqs, oploop = self.__get_eqs_and_oploop(
            do_ratespec, do_ropsplit, do_spec_per_reac,
            do_vector=False)

        reacs = self.store.reacs
        specs = self.store.specs

        exceptions = ['device', 'conp']

        for i, state in enumerate(oploop):
            if state['width'] is not None and state['depth'] is not None:
                continue
            opt = loopy_options(**{x: state[x] for x in
                                   state if x not in exceptions})
            # find rate info
            rate_info = assign_rates(reacs, specs, opt.rate_spec)
            try:
                conp = state['conp']
            except:
                conp = True
            # create namestore
            namestore = arc.NameStore(opt, rate_info, conp,
                                      self.store.test_size)
            # create the kernel info
            infos = func(eqs, opt, namestore,
                         test_size=self.store.test_size, **kw_args)

            if not isinstance(infos, list):
                try:
                    infos = list(infos)
                except:
                    infos = [infos]

            # create a dummy kernel generator
            knl = k_gen.make_kernel_generator(
                name='spec_rates',
                loopy_opts=opt,
                kernels=infos,
                test_size=self.store.test_size
            )

            knl._make_kernels()

            # create a list of answers to check
            try:
                for kc in kernel_calls:
                    kc.set_state(state['order'])
            except:
                kernel_calls.set_state(state['order'])

            assert auto_run(knl.kernels, kernel_calls, device=opt.device),\
                'Evaluate {} rates failed'.format(func.__name__)

    def _make_array(self, array):
        """
        Creates an array for comparison to an autorun kernel from the result
        of __get_jacobian

        Parameters
        ----------
        array : :class:`numpy.ndarray`
            The input Jacobian array

        Returns
        -------
        reshaped : :class:`numpy.ndarray`
            The reshaped  / reordered array for comparison to the autorun
            kernel
        """

        for i in range(array.shape[0]):
            # reshape inner array
            array[i, :, :] = np.reshape(array[i, :, :].flatten(order='K'),
                                        array.shape[1:],
                                        order='F')

        return array

    def _get_jacobian(self, func, kernel_call, editor, ad_opts, conp,
                      extra_funcs=[],
                      **kw_args):
        eqs = {'conp': self.store.conp_eqs,
               'conv': self.store.conv_eqs}
        # find rate info
        rate_info = assign_rates(
            self.store.reacs,
            self.store.specs,
            ad_opts.rate_spec)
        # create namestore
        namestore = arc.NameStore(ad_opts, rate_info, conp,
                                  self.store.test_size)

        # get kw args this function expects
        def __get_arg_dict(check, **in_args):
            try:
                arg_count = check.func_code.co_argcount
                args = check.func_code.co_varnames[:arg_count]
            except:
                arg_count = check.__code__.co_argcount
                args = check.__code__.co_varnames[:arg_count]

            args_dict = {}
            for k, v in six.iteritems(in_args):
                if k in args:
                    args_dict[k] = v
            return args_dict

        # create the kernel info
        infos = []
        info = func(eqs, ad_opts, namestore,
                    test_size=self.store.test_size,
                    **__get_arg_dict(func, **kw_args))
        try:
            infos.extend(info)
        except:
            infos.append(info)

        # create a dummy kernel generator
        knl = k_gen.make_kernel_generator(
            name='jacobian',
            loopy_opts=ad_opts,
            kernels=infos,
            test_size=self.store.test_size,
            extra_kernel_data=[editor.output]
        )
        knl._make_kernels()

        # get list of current args
        new_args = []
        for k in knl.kernels:
            for arg in k.args:
                if arg not in new_args and not isinstance(
                        arg, lp.TemporaryVariable):
                    new_args.append(arg)

        # generate dependencies with full test size to get extra args
        for f in extra_funcs:
            info = f(eqs, ad_opts, namestore,
                     test_size=self.store.test_size,
                     **__get_arg_dict(f, **kw_args))
            infos = []
            try:
                infos.extend(info)
            except:
                infos.append(info)
            for i in infos:
                for arg in i.kernel_data:
                    if arg not in new_args and not isinstance(
                            arg, lp.TemporaryVariable):
                        new_args.append(arg)

        knl.kernels[0] = knl.kernels[0].copy(args=new_args[:])

        # and a generator for the single kernel
        single_name = arc.NameStore(ad_opts, rate_info, conp, 1)
        single_info = []
        for f in extra_funcs + [func]:
            i = f(eqs, ad_opts, single_name,
                  test_size=1,
                  **__get_arg_dict(f, **kw_args))
            try:
                single_info.extend(i)
            except:
                single_info.append(i)

        single_knl = k_gen.make_kernel_generator(
            name='spec_rates',
            loopy_opts=ad_opts,
            kernels=single_info,
            test_size=1,
            extra_kernel_data=[editor.output]
        )

        single_knl._make_kernels()

        # set in editor
        editor.set_single_kernel(single_knl.kernels)

        kernel_call.set_state(ad_opts.order)

        # add dummy 'j' arguement
        kernel_call.kernel_args['j'] = -1
        # and place output
        kernel_call.kernel_args[editor.output.name] = np.zeros(
            editor.output.shape,
            order=editor.output.order)

        # run kernel
        populate(
            knl.kernels, kernel_call,
            editor=editor)

        return self._make_array(kernel_call.kernel_args[editor.output.name])

    def _make_namestore(self, conp):
        # get number of sri reactions
        reacs = self.store.reacs
        specs = self.store.specs
        rate_info = assign_rates(reacs, specs, RateSpecialization.fixed)

        ad_opts = loopy_options(order='C', knl_type='map', lang='c',
                                auto_diff=True)

        # create namestore
        namestore = arc.NameStore(ad_opts,
                                  rate_info, conp, self.store.test_size)

        return namestore, rate_info

    @attr('long')
    def test_dropi_dnj(self):

        # test conp
        namestore, rate_info = self._make_namestore(True)
        ad_opts = namestore.loopy_opts

        # set up arguements
        allint = {'net': rate_info['net']['allint']}
        # create the editor
        edit = editor(
            namestore.n_arr, namestore.n_dot, self.store.test_size,
            order=ad_opts.order)

        args = {'rop_fwd': lambda x: np.zeros_like(
            self.store.fwd_rxn_rate, order=x),
            'rop_rev': lambda x: np.zeros_like(
            self.store.rev_rxn_rate, order=x),
            'pres_mod': lambda x: np.array(
            self.store.ref_pres_mod, order=x, copy=True),
            'rop_net': lambda x: np.zeros_like(
            self.store.rxn_rates, order=x),
            'phi': lambda x: np.array(
            self.store.phi_cp, order=x, copy=True),
            'P_arr': lambda x: np.array(
            self.store.P, order=x, copy=True),
            'kf': lambda x: np.array(
            self.store.fwd_rate_constants, order=x, copy=True),
            'kr': lambda x: np.array(
            self.store.rev_rate_constants, order=x, copy=True),
            'conc': lambda x: np.zeros_like(
            self.store.concs, order=x),
            'wdot': lambda x: np.zeros_like(
            self.store.species_rates, order=x),
            'jac': lambda x: np.zeros(namestore.jac.shape, order=x)
        }

        # obtain the finite difference jacobian
        kc = kernel_call('dRopidnj', [self.store.rxn_rates], **args)

        fd_jac = self._get_jacobian(
            get_molar_rates, kc, edit, ad_opts, True,
            extra_funcs=[get_concentrations, get_rop, get_rop_net,
                         get_spec_rates],
            do_not_set=[namestore.rop_fwd, namestore.rop_rev,
                        namestore.conc_arr, namestore.spec_rates,
                        namestore.presmod],
            allint=allint)

        def _chainer(self, out_vals):
            self.kernel_args['jac'] = out_vals[-1][0].copy(
                order=self.current_order)

        jac_size = rate_info['Ns'] + 1
        args = {
            'kf': lambda x: np.array(
                self.store.fwd_rate_constants, order=x, copy=True),
            'kr': lambda x: np.array(
                self.store.rev_rate_constants, order=x, copy=True),
            'pres_mod': lambda x: np.array(
                self.store.ref_pres_mod, order=x, copy=True),
            'conc': lambda x: np.array(
                self.store.concs, order=x, copy=True),
            'jac': lambda x: np.zeros(
                (self.store.test_size, jac_size, jac_size), order=x)
        }
        # and test
        kc = [kernel_call('dRopidnj', [fd_jac], check=False,
                          strict_name_match=True, **args),
              kernel_call('dRopidnj_ns', [fd_jac], compare_mask=[(
                  2 + np.arange(self.store.gas.n_species - 1),
                  2 + np.arange(self.store.gas.n_species - 1))],
            compare_axis=(1, 2), chain=_chainer, strict_name_match=True,
            **args)]

        return self._generic_jac_tester(dRopi_dnj, kc, allint=allint)

    @attr('long')
    def test_dci_thd_dnj(self):
        # test conp
        namestore, rate_info = self._make_namestore(True)
        ad_opts = namestore.loopy_opts

        # setup arguemetns
        # create the editor
        edit = editor(
            namestore.n_arr, namestore.n_dot, self.store.test_size,
            order=ad_opts.order)

        args = {'rop_fwd': lambda x: np.array(
            self.store.fwd_rxn_rate, order=x, copy=True),
            'rop_rev': lambda x: np.array(
            self.store.rev_rxn_rate, order=x, copy=True),
            'pres_mod': lambda x: np.array(
            self.store.ref_pres_mod, order=x, copy=True),
            'rop_net': lambda x: np.zeros_like(
            self.store.rxn_rates, order=x),
            'phi': lambda x: np.array(
            self.store.phi_cp, order=x, copy=True),
            'P_arr': lambda x: np.array(
            self.store.P, order=x, copy=True),
            'kf': lambda x: np.array(
            self.store.fwd_rate_constants, order=x, copy=True),
            'kr': lambda x: np.array(
            self.store.rev_rate_constants, order=x, copy=True),
            'conc': lambda x: np.zeros_like(
            self.store.concs, order=x),
            'wdot': lambda x: np.zeros_like(
            self.store.species_rates, order=x),
            'thd_conc': lambda x: np.zeros_like(
            self.store.ref_thd, order=x),
            'Fi': lambda x: np.array(
            self.store.ref_Fall, order=x, copy=True),
            'Pr': lambda x: np.array(
            self.store.ref_Pr, order=x, copy=True),
            'jac': lambda x: np.zeros(namestore.jac.shape, order=x)
        }

        # obtain the finite difference jacobian
        kc = kernel_call('dci_thd_nj', [None], **args)

        fd_jac = self._get_jacobian(
            get_molar_rates, kc, edit, ad_opts, True,
            extra_funcs=[get_concentrations, get_thd_body_concs,
                         get_rxn_pres_mod, get_rop_net,
                         get_spec_rates],
            do_not_set=[
                namestore.conc_arr, namestore.spec_rates, namestore.rop_net])

        # setup args
        jac_size = rate_info['Ns'] + 1
        args = {
            'rop_net': lambda x: np.array(
                self.store.rxn_rates, order=x, copy=True),
            'jac': lambda x: np.zeros(
                (self.store.test_size, jac_size, jac_size), order=x)
        }

        # get list of species not in falloff / chemically activated
        # to get the check mask
        in_falloff = set()
        for rxn in self.store.gas.reactions():
            specs = set(rxn.products.keys()) | set(rxn.reactants.keys())
            nonzero_specs = set()
            if isinstance(rxn, ct.FalloffReaction):
                for spec in specs:
                    nu = 0
                    if spec in rxn.products:
                        nu += rxn.products[spec]
                    if spec in rxn.reactants:
                        nu -= rxn.reactants[spec]
                    if nu != 0:
                        nonzero_specs.update([spec])
                if isinstance(rxn, ct.FalloffReaction):
                    in_falloff.update(nonzero_specs)
        not_in_falloff = set(self.store.gas.species_names) - in_falloff - \
            set([self.store.gas.species_names[-1]])
        not_in_falloff = np.array(sorted([self.store.gas.species_index(x)
                                          for x in not_in_falloff])) + 2

        def _chainer(self, out_vals):
            self.kernel_args['jac'] = out_vals[-1][0].copy(
                order=self.current_order)

        # and get mask
        kc = [kernel_call('dci_thd_dnj', [fd_jac], check=False,
                          strict_name_match=True, **args),
              kernel_call('dci_thd_dnj_ns', [fd_jac], compare_mask=[(
                  not_in_falloff,
                  2 + np.arange(self.store.gas.n_species - 1))],
            compare_axis=(1, 2), chain=_chainer, strict_name_match=True,
            **args)]

        return self._generic_jac_tester(dci_thd_dnj, kc)

    @attr('long')
    def test_sri_derivatives(self):
        ref_phi = self.store.phi
        ref_Pr = self.store.ref_Pr.copy()
        ref_ans = self.store.ref_Sri.copy().squeeze()
        args = {'Pr': lambda x: np.array(ref_Pr, order=x, copy=True),
                'phi': lambda x: np.array(ref_phi, order=x, copy=True),
                }

        conp = True

        # get number of sri reactions
        namestore, rate_info = self._make_namestore(conp)
        ad_opts = namestore.loopy_opts

        sri_inds = np.arange(rate_info['fall']['sri']['map'], dtype=np.int32)
        if not sri_inds.size:
            return
        # create the kernel calls
        kc = kernel_call('fall_sri', ref_ans, out_mask=[1],
                         compare_mask=[sri_inds], **args)

        # create editor
        myedit = editor(namestore.T_arr, namestore.Fi,
                        self.store.test_size, order=ad_opts.order,
                        do_not_set=namestore.X_sri)

        answer = self._get_jacobian(get_sri_kernel, kc, myedit, conp, ad_opts)

        kc.ref_answer = answer
