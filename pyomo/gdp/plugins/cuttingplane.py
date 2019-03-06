#  ___________________________________________________________________________
#
#  Pyomo: Python Optimization Modeling Objects
#  Copyright 2017 National Technology and Engineering Solutions of Sandia, LLC
#  Under the terms of Contract DE-NA0003525 with National Technology and
#  Engineering Solutions of Sandia, LLC, the U.S. Government retains certain
#  rights in this software.
#  This software is distributed under the 3-clause BSD License.
#  ___________________________________________________________________________

"""
Cutting plane-based GDP reformulation.

Implements a general cutting plane-based reformulation for linear and
convex GDPs.
"""
from __future__ import division
try:
    from collections import OrderedDict
except:
    from ordereddict import OrderedDict


from pyomo.common.config import ConfigBlock, ConfigValue, PositiveFloat
from pyomo.common.modeling import unique_component_name
from pyomo.core import (
    Any, Block, Constraint, Objective, Param, Var, SortComponents,
    Transformation, TransformationFactory, value, TransformationFactory,
)
from pyomo.core.base.symbolic import differentiate
from pyomo.core.base.component import ComponentUID
from pyomo.core.expr.current import identify_variables
from pyomo.repn.standard_repn import generate_standard_repn
from pyomo.core.kernel.component_map import ComponentMap
from pyomo.core.kernel.component_set import ComponentSet
from pyomo.opt import SolverFactory

from pyomo.gdp import Disjunct, Disjunction, GDP_Error
from pyomo.gdp.util import (
    verify_successful_solve, NORMAL, INFEASIBLE, NONOPTIMAL
)

from six import iterkeys, itervalues
from numpy import isclose

import math
import logging
logger = logging.getLogger('pyomo.gdp.cuttingplane')

# DEBUG
from nose.tools import set_trace

# TODO: this should be an option probably, right?
# do I have other options that won't be mad about the quadratic objective in the
# separation problem?
SOLVER = 'ipopt'
stream_solvers = False


@TransformationFactory.register('gdp.cuttingplane',
                                doc="Relaxes a linear disjunctive model by "
                                "adding cuts from convex hull to Big-M "
                                "relaxation.")
class CuttingPlane_Transformation(Transformation):

    CONFIG = ConfigBlock("gdp.cuttingplane")
    CONFIG.declare('solver', ConfigValue(
        default='ipopt',
        domain=str,
        description="""Solver to use for relaxed BigM problem and the separation
        problem""",
        doc="""
        This specifies the solver which will be used to solve LP relaxation
        of the BigM problem and the separation problem. Note that this solver
        must be able to handle a quadratic objective because of the separation
        problem.
        """
    ))
    CONFIG.declare('EPS', ConfigValue(
        default=0.05,#TODO: this is an experiment... 0.01,
        domain=PositiveFloat,
        description="Epsilon value used to decide when to stop adding cuts",
        doc="""
        If the difference between the objectives in two consecutive iterations is
        less than this value, the algorithm terminates without adding the cut
        generated in the last iteration.  """
    ))
    CONFIG.declare('stream_solver', ConfigValue(
        default=False,
        domain=bool,
        description="""If true, sets tee=True for every solve performed over
        "the course of the algorithm"""
    ))
    CONFIG.declare('solver_options', ConfigValue(
        default={},
        description="Dictionary of solver options",
        doc="""
        Dictionary of solver options that will be set for the solver for both the
        relaxed BigM and separation problem solves.
        """
    ))

    def __init__(self):
        super(CuttingPlane_Transformation, self).__init__()

    def _apply_to(self, instance, bigM=None, **kwds):
        self._config = self.CONFIG(kwds.pop('options', {}))
        self._config.set_value(kwds)

        (instance_rBigM, instance_rCHull, var_info, var_map,
         disaggregated_var_info, transBlockName) = self._setup_subproblems(
             instance, bigM)

        self._generate_cuttingplanes( instance, instance_rBigM, instance_rCHull,
                                      var_info, var_map, disaggregated_var_info,
                                      transBlockName)

    def _setup_subproblems(self, instance, bigM):
        # create transformation block
        transBlockName, transBlock = self._add_relaxation_block(
            instance,
            '_pyomo_gdp_cuttingplane_relaxation')

        # We store a list of all vars so that we can efficiently
        # generate maps among the subproblems
        transBlock.all_vars = list(v for v in instance.component_data_objects(
            Var,
            descend_into=(Block, Disjunct),
            sort=SortComponents.deterministic) if not v.is_fixed())

        # we'll store all the cuts we add together
        transBlock.cuts = Constraint(Any)

        # get bigM and chull relaxations
        bigMRelaxation = TransformationFactory('gdp.bigm')
        chullRelaxation = TransformationFactory('gdp.chull')
        relaxIntegrality = TransformationFactory('core.relax_integrality')

        # HACK: for the current writers, we need to also apply gdp.reclassify so
        # that the indicator variables stay where they are in the big M model
        # (since that is what we are eventually going to solve after we add our
        # cuts).
        reclassify = TransformationFactory('gdp.reclassify')

        #
        # Generate the CHull relaxation (used for the separation
        # problem to generate cutting planes)
        #
        instance_rCHull = chullRelaxation.create_using(instance)
        # This relies on relaxIntegrality relaxing variables on deactivated
        # blocks, which should be fine.
        reclassify.apply_to(instance_rCHull)
        relaxIntegrality.apply_to(instance_rCHull)

        #
        # Reformulate the instance using the BigM relaxation (this will
        # be the final instance returned to the user)
        #
        bigMRelaxation.apply_to(instance, bigM=bigM)
        reclassify.apply_to(instance)

        #
        # Generate the continuous relaxation of the BigM transformation
        #
        instance_rBigM = relaxIntegrality.create_using(instance)

        #
        # Add the xstar parameter for the CHull problem
        #
        transBlock_rCHull = instance_rCHull.component(transBlockName)
        #
        # this will hold the solution to rbigm each time we solve it. We
        # add it to the transformation block so that we don't have to
        # worry about name conflicts.
        transBlock_rCHull.xstar = Param(
            range(len(transBlock.all_vars)), mutable=True, default=None)
        # we will add a block that we will deactivate to use to store the
        # extended space cuts. We never need to solve these, but we need them to
        # be contructed for the sake of Fourier-Motzkin Elimination
        extendedSpaceCuts = transBlock_rCHull.extendedSpaceCuts = Block()
        extendedSpaceCuts.deactivate()
        extendedSpaceCuts.cuts = Constraint(Any)

        transBlock_rBigM = instance_rBigM.component(transBlockName)

        # create a map which links all disaggregated variables to their
        # originals on both bigm and rBigm. We will use this to project the cut
        # from the extended space to the space of the bigM problem.
        disaggregatedVarMap = self._get_disaggregated_var_map(instance_rCHull,
                                                              instance,
                                                              instance_rBigM)

        #
        # Generate the mapping between the variables on all the
        # instances and the xstar parameter.
        #
        var_info = tuple(
            (v,
             transBlock_rBigM.all_vars[i],
             transBlock_rCHull.all_vars[i],
             transBlock_rCHull.xstar[i])
            for i,v in enumerate(transBlock.all_vars))

        # TODO: I don't know a better way to do this
        disaggregated_var_info = tuple(
            (v,
             disaggregatedVarMap[v]['bigm'],
             disaggregatedVarMap[v]['rBigm'])
            for v in disaggregatedVarMap.keys())

        # this is the map that I need to translate my projected cuts and add
        # them to bigM and rBigM.
        # TODO: If I had xstar to this (or don't) can I just replace var_info?
        var_map = ComponentMap((transBlock_rCHull.all_vars[i],
                                {'bigM': v,
                                 'rBigM': transBlock_rBigM.all_vars[i]})
                               for i,v in enumerate(transBlock.all_vars))

        #
        # Add the separation objective to the chull subproblem
        #
        self._add_separation_objective(var_info, transBlock_rCHull)

        return (instance_rBigM, instance_rCHull, var_info, var_map,
                disaggregated_var_info, transBlockName)

    def _get_disaggregated_var_map(self, chull, bigm, rBigm):
        disaggregatedVarMap = ComponentMap()
        # TODO: I guess technically I don't know that the transformation block
        # is named this... It could have a unique name, so I need to hunt that
        # down. (And then test that I do that correctly)
        for disjunct in chull._pyomo_gdp_chull_relaxation.relaxedDisjuncts.\
            values():
            for disaggregated_var, original in \
            disjunct._gdp_transformation_info['srcVars'].iteritems():
                orig_vars = disaggregatedVarMap.get(disaggregated_var)
                if orig_vars is None:
                    # TODO: this is probably expensive, but I don't have another
                    # idea...
                    orig_cuid = ComponentUID(original)
                    disaggregatedVarMap[disaggregated_var] = \
                                    {'bigm': orig_cuid.find_component(bigm),
                                     'rBigm': orig_cuid.find_component(rBigm)}

        return disaggregatedVarMap

    def _generate_cuttingplanes(
            self, instance, instance_rBigM, instance_rCHull,
            var_info, var_map, disaggregated_var_info, transBlockName):

        opt = SolverFactory(self._config.solver)
        stream_solver = self._config.stream_solver
        opt.options = self._config.solver_options

        improving = True
        prev_obj = float("inf")
        epsilon = self._config.EPS
        cuts = None

        transBlock = instance.component(transBlockName)
        transBlock_rBigM = instance_rBigM.component(transBlockName)
        transBlock_rCHull = instance_rCHull.component(transBlockName)

        # We try to grab the first active objective. If there is more
        # than one, the writer will yell when we try to solve below. If
        # there are 0, we will yell here.
        rBigM_obj = next(instance_rBigM.component_data_objects(
            Objective, active=True), None)
        if rBigM_obj is None:
            raise GDP_Error("Cannot apply cutting planes transformation "
                            "without an active objective in the model!")

        # Get list of all variables in the rCHull model which we will use when
        # calculating the composite normal vector.
        rCHull_vars = [i for i in instance_rCHull.component_data_objects(
            Var,
            descend_into=Block,
            sort=SortComponents.deterministic)]

        while (improving):
            # solve rBigM, solution is xstar
            results = opt.solve(instance_rBigM, tee=stream_solver)
            if verify_successful_solve(results) is not NORMAL:
                logger.warning("GDP.cuttingplane: Relaxed BigM subproblem "
                               "did not solve normally. Stopping cutting "
                               "plane generation.\n\n%s" % (results,))
                return

            rBigM_objVal = value(rBigM_obj)
            logger.warning("gdp.cuttingplane: rBigM objective = %s"
                           % (rBigM_objVal,))

            # copy over xstar
            # DEBUG
            print("x* is")
            for x_bigm, x_rbigm, x_chull, x_star in var_info:
                x_star.value = x_rbigm.value
                # initialize the X values
                x_chull.value = x_rbigm.value
                # DEBUG
                print("\t%s: %s" % (x_rbigm.name, x_star.value))

            # compare objectives: check absolute difference close to 0, relative
            # difference further from 0.
            obj_diff = prev_obj - rBigM_objVal
            improving = math.isinf(obj_diff) or \
                        ( abs(obj_diff) > epsilon if abs(rBigM_objVal) < 1 else
                          abs(obj_diff/prev_obj) > epsilon )

            # solve separation problem to get xhat.
            opt.solve(instance_rCHull, tee=stream_solver)
            # DEBUG
            print("x_hat is")
            for x_hat in rCHull_vars:
               print("\t%s: %s" % (x_hat.name, x_hat.value))
            print "Separation obj = %s" % (
               value(next(instance_rCHull.component_data_objects(
               Objective, active=True))),)

            # [JDS 19 Dec 18] Note: we should check that the separation
            # objective was significantly nonzero.  If it is too close
            # to zero, either the rBigM solution was in the convex hull,
            # or the separation vector is so close to zero that the
            # resulting cut is likely to have numerical issues.
            if abs(value(transBlock_rCHull.separation_objective)) < epsilon:
                # [ESJ 15 Feb 19] I think we just want to quit right, we're
                # going nowhere...?
                break

            cuts = self._create_cuts(var_info, var_map, disaggregated_var_info,
                                     rCHull_vars, instance_rCHull, transBlock,
                                     transBlock_rBigM, transBlock_rCHull)
           
            # We are done if the cut generator couldn't return a valid cut
            if not cuts:
                break

            # add cut to rBigm
            for cut in cuts['rBigM']:
                transBlock_rBigM.cuts.add(len(transBlock_rBigM.cuts), cut)

            # DEBUG
            #print("adding this cut to rBigM:\n%s <= 0" % cuts['rBigM'])

            if improving:
                for cut in cuts['bigM']:
                    cut_number = len(transBlock.cuts)
                    logger.warning("GDP.cuttingplane: Adding cut %s to BM model."
                                   % (cut_number,))
                    transBlock.cuts.add(cut_number, cut)

            prev_obj = rBigM_objVal


    def _add_relaxation_block(self, instance, name):
        # creates transformation block with a unique name based on name, adds it
        # to instance, and returns it.
        transBlockName = unique_component_name(
            instance,
            '_pyomo_gdp_cuttingplane_relaxation')
        transBlock = Block()
        instance.add_component(transBlockName, transBlock)
        return transBlockName, transBlock


    def _add_separation_objective(self, var_info, transBlock_rCHull):
        # Deactivate any/all other objectives
        for o in transBlock_rCHull.model().component_data_objects(Objective):
            o.deactivate()

        obj_expr = 0
        for x_bigm, x_rbigm, x_chull, x_star in var_info:
            obj_expr += (x_chull - x_star)**2
        # add separation objective to transformation block
        transBlock_rCHull.separation_objective = Objective(expr=obj_expr)


    def _create_cuts(self, var_info, var_map, disaggregated_var_info, 
                     rCHull_vars, instance_rCHull, transBlock, transBlock_rBigm,
                     transBlock_rCHull):
        cut_number = len(transBlock.cuts)
        logger.warning("gdp.cuttingplane: Creating (but not yet adding) cut %s."
                       % (cut_number,))
        # DEBUG
        # print("CURRENT SOLN (to separation problem):")
        # for var in rCHull_vars:
        #     print(var.name + '\t' + str(value(var)))

        # loop through all constraints in rCHull and figure out which are active
        # or slightly violated. For each we will get the tangent plane at xhat
        # (which is x_chull below). We get the normal vector for each of these
        # tangent planes and sum them to get a composite normal. Our cut is then
        # the hyperplane normal to this composite through xbar (projected into
        # the original space).
        normal_vectors = []
        # DEBUG
        # print("-------------------------------")
        # print("These constraints are tight:")
        #print "POINT: ", [value(_) for _ in rCHull_vars]
        tight_constraints = []
        for constraint in instance_rCHull.component_data_objects(
                Constraint,
                active=True,
                descend_into=Block,
                sort=SortComponents.deterministic):
            print "   CON: ", constraint.expr
            multiplier = self.constraint_tight(instance_rCHull, constraint)
            if multiplier:
                tight_constraints.append(
                    self.get_linear_constraint_repn(constraint))
                # DEBUG
                # print(constraint.name)
                # print constraint.expr
                # get normal vector to tangent plane to this constraint at xhat
                print "      TIGHT", multiplier
                f = constraint.body
                firstDerivs = differentiate(f, wrt_list=rCHull_vars)
                #print "     ", firstDerivs
                normal_vectors.append(
                    [multiplier*value(_) for _ in firstDerivs])

        # It is possible that the separation problem returned a point in
        # the interior of the convex hull.  It is also possible that the
        # only active constraints are (feasible) equality constraints.
        # in these situations, there are no normal vectors from which to
        # create a valid cut.
        if not normal_vectors:
            return None

        composite_normal = list(
            sum(_) for _ in zip(*tuple(normal_vectors)) )
        composite_normal_map = ComponentMap(
            (v,n) for v,n in zip(rCHull_vars, composite_normal))

        # DEBUG
        print "COMPOSITE NORMAL, cut number %s" % cut_number
        for x,v in composite_normal_map.iteritems():
            print(x.name + '\t' + str(v))

        # add a cut which is tangent to the composite normal at xhat:
        # (we are projecting out the disaggregated variables)
        # composite_cutexpr_bigm = 0
        # composite_cutexpr_rBigM = 0
        # projection_cutexpr_bigm = 0
        # projection_cutexpr_rBigM = 0
        composite_cutexpr_CHull = 0
        # TODO: I don't think we need x_star in var_info anymore. Or maybe at
        # all?
        # DEBUG:
        #print composite_normal
        #print("FOR COMPARISON:\ncomposite\tx_hat - xstar")
        for x_bigm, x_rbigm, x_chull, x_star in var_info:
            # make the cut in the CHull space with the CHull variables. We will
            # translate it all to BigM and rBigM later when we have projected
            # out the disaggregated variables
            composite_cutexpr_CHull += composite_normal_map[x_chull]*\
                                       (x_chull - x_chull.value)

            # composite_cutexpr_bigm \
            #     += composite_normal_map[x_chull]*(x_bigm - x_chull.value)
            # composite_cutexpr_rBigM \
            #     += composite_normal_map[x_chull]*(x_rbigm - x_chull.value)

            # # DEBUG: old way
            # projection_cutexpr_bigm += 2*(x_star.value - x_chull.value)*\
            #                            (x_bigm - x_chull.value)
            # projection_cutexpr_rBigM += 2*(x_star.value - x_chull.value)*\
            #                             (x_rbigm - x_chull.value)
            # DEBUG:
            # print("%s\t%s" %
            #       (composite_normal[x_chull], x_star.value - x_chull.value))

        # I am going to expand the composite_cutexprs to be in the extended space
        vars_to_eliminate = ComponentSet()
        for x_disaggregated, x_orig_bigm, x_orig_rBigm in disaggregated_var_info:
            composite_cutexpr_CHull += composite_normal_map[x_disaggregated]*\
                                       (x_disaggregated - x_disaggregated.value)
            vars_to_eliminate.add(x_disaggregated)
    
        print("The cut in extended space is: %s <= 0" % composite_cutexpr_CHull)
        cut_std_repn = generate_standard_repn(composite_cutexpr_CHull)
        cut_cons = {'lower': None, 
                    'upper': 0, 
                    'body': ComponentMap(zip(cut_std_repn.linear_vars,
                                          cut_std_repn.linear_coefs))}
        cut_cons['body'][None] = value(cut_std_repn.constant)
        tight_constraints.append(cut_cons)
        
        # [ESJ 22 Feb 2019] TODO: It is probably worth checking that the
        # disaggregated vars actually appear in the cut before we bother with
        # FME
        projected_constraints = self.fourier_motzkin_elimination(
            tight_constraints, vars_to_eliminate)

        # DEBUG:
        print("These are the constraints we got from FME:")
        for cons in projected_constraints:
            body = 0
            for var, val in cons['body'].items():
                body += val*var if var is not None else val
            print("\t%s <= %s <= %s" % (cons['lower'], body, cons['upper']))

        # we created these constraints with the variables from rCHull. We
        # actually need constraints for BigM and rBigM now!
        cuts = self.get_constraint_exprs(projected_constraints, var_map)

        # for debugging, I think I want to add all the constraints just so I can
        # see them. I can deactivate the ones I don't like.

        #print "Composite normal cut"
        #print "   %s" % (composite_cutexpr_rBigM,)
        #print "optimal sol'n for rBigm"
        #instance_rCHull._pyomo_gdp_cuttingplane_relaxation.xstar.pprint()

        # print "Calculating the cut the old way we have:"
        # print "   %s" % (projection_cutexpr_rBigM,)
        # DEBUG
        # print("++++++++++++++++++++++++++++++++++++++++++")
        # print("So this is the cut expression:")
        # print(cutexpr_bigm)

        # cuts = self._project_cuts_to_bigM_space(
        #     {'bigm': composite_cutexpr_bigm, 'rBigM': composite_cutexpr_rBigM},
        #     composite_normal_map, disaggregated_var_info)

        #return({'bigm': projection_cutexpr_bigm,
        #        'rBigM': projection_cutexpr_rBigM})
        return(cuts)

    def get_constraint_exprs(self, constraints, var_map):
        print("==========================\nBuilding actual expressions")
        cuts = {}
        cuts['rBigM'] = []
        cuts['bigM'] = []
        for cons in constraints:
            # DEBUG
            print("cons:")
            body = 0
            for var, val in cons['body'].items():
                body += val*var if var is not None else val
            print("\t%s <= %s <= %s" % (cons['lower'], body, cons['upper']))
            body_bigM = 0
            body_rBigM = 0
            # TODO: you need to check if this constraint actually has a body. If
            # not, that is what is getting the error about the boolean, and you
            # don't want it anyway!
            trivial_constraint = True
            for var, coef in cons['body'].items():
                if var is None:
                    body_bigM += coef
                    body_rBigM += coef
                    continue
                # TODO: do I want almost equal here? I'm going to crash if I get
                # one of the disaggagregated variables... In case it didn't
                # quite cancel?
                if coef != 0:
                    body_bigM += coef*var_map[var]['bigM']
                    body_rBigM += coef*var_map[var]['rBigM']
                    trivial_constraint = False
            if trivial_constraint:
                continue
            if cons['lower'] is not None:
                cuts['rBigM'].append(cons['lower'] <= body_rBigM)
                cuts['bigM'].append(cons['lower'] <= body_bigM)
            elif cons['upper'] is not None:
                cuts['rBigM'].append(cons['upper'] >= body_rBigM)
                cuts['bigM'].append(cons['upper'] >= body_bigM)
        return cuts


    # assumes that constraints is a list of my own linear constraint repn (see
    # below)
    def fourier_motzkin_elimination(self, constraints, vars_to_eliminate):
        # First we will preprocess so that we have no equalities (break them
        # into two constraints)
        tmpConstraints = [cons for cons in constraints]
        for cons in tmpConstraints:
            if cons['lower'] is not None and cons['upper'] is not None:
                # make a copy to become the geq side 
                geq = {'lower': None,
                       'upper': cons['upper'],
                       # I'm doing this so that I know I have a copy:
                       'body': ComponentMap(
                           (var, coef) for (var, coef) in cons['body'].items()) 
                }
                cons['upper'] = None
                constraints.append(geq)

        # DEBUG
        print("Checking constraints we are passing into recursive thing:")
        for cons in constraints:
            body = 0
            for var, val in cons['body'].items():
                body += val*var if var is not None else val
            print("\t%s <= %s <= %s" % (cons['lower'], body, cons['upper']))
        print("OK, are they right??")
                
        return self.fm_elimination(constraints, vars_to_eliminate)

    def fm_elimination(self, constraints, vars_to_eliminate):
        if not vars_to_eliminate:
            return constraints
        
        the_var = vars_to_eliminate.pop()
        print("DEBUG: we are eliminating %s" % the_var.name)
        # we are 'reorganizing' the constraints, we will map the coefficient of
        # the_var from that constraint and the rest of the expression and sorting
        # based on whether we have the_var <= other stuff or vice versa.
        leq_list = []
        geq_list = []
        waiting_list = []

        # sort our constraints, make it so leq constraints have coef of -1 on
        # variable to eliminate, geq constraints have coef of 1 (so we can add
        # them)
        #DEBUG
        print("CONSTRAINTS:")
        while(constraints):
            cons = constraints.pop()
        #for cons in constraints:
            # DEBUG
            body = 0
            for var, val in cons['body'].items():
                body += val*var if var is not None else val
            print("\t%s <= %s <= %s" % (cons['lower'], body, cons['upper']))

            leaving_var_coef = cons['body'].get(the_var)
            if leaving_var_coef is None or leaving_var_coef == 0:
                waiting_list.append(cons)
                print("\tskipping")
                continue

            if cons['lower'] is not None:
                if leaving_var_coef < 0:
                    # don't flip the sign
                    leq_list.append(self.scalar_multiply_linear_constraint(
                        cons, -1.0/leaving_var_coef))
                    print("\tleq (lower)")
                else:
                    # don't flip the sign
                    geq_list.append(self.scalar_multiply_linear_constraint(
                        cons, 1.0/leaving_var_coef))
                    print("\tgeq (lower)")

            # NOTE: this else matters because we are changing the constraint
            # when we flip it!!
            elif cons['upper'] is not None:
                if leaving_var_coef > 0:
                    # flip the sign
                    leq_list.append(self.scalar_multiply_linear_constraint(
                        cons, -1.0/leaving_var_coef))
                    print("\tgeq (upper)")
                else:
                    # flip the sign
                    geq_list.append(self.scalar_multiply_linear_constraint(
                        cons, 1.0/leaving_var_coef))
                    print("\tgeq (upper)")
            #constraints.remove(cons)

        print("Here be leq constraints:")
        for cons in leq_list:
            body = 0
            for var, val in cons['body'].items():
                body += val*var if var is not None else val
            print("\t%s <= %s <= %s" % (cons['lower'], body, cons['upper']))

        print("Here be geq constraints:")
        for cons in geq_list:
            body = 0
            for var, val in cons['body'].items():
                body += val*var if var is not None else val
            print("\t%s <= %s <= %s" % (cons['lower'], body, cons['upper']))   

        for leq in leq_list:
            for geq in geq_list:
                constraints.append(self.add_linear_constraints(leq, geq))

        # add back in the constraints that didn't have the variable we were
        # projecting out
        for cons in waiting_list:
            constraints.append(cons)

        print("This is what we have now:")
        for cons in constraints:
            body = 0
            for var, val in cons['body'].items():
                body += val*var if var is not None else val
            print("\t%s <= %s <= %s" % (cons['lower'], body, cons['upper']))
        
        return self.fm_elimination(constraints, vars_to_eliminate)

    def _project_cuts_to_bigM_space(self, cuts, normal_map,
                                    disaggregated_var_info):
        # Cut generator didn't return a valid cut, so there is nothing to
        # project and we are done.
        if not cuts:
            return None

        bigm_cut = cuts['bigm']
        # DEBUG:
        print "Cut we will project:"
        print bigm_cut
        rBigm_cut = cuts['rBigM']
        expr_data = ComponentMap()
        for x_disaggregated, x_orig_bigm, x_orig_rBigm in disaggregated_var_info:
            data = expr_data.get(x_orig_bigm)
            print("%s\t%s\t%s" % (x_disaggregated.name,
                                  normal_map[x_disaggregated],
                                  x_disaggregated.value))
            if data is None:
                data = expr_data[x_orig_bigm] = \
                       {'coef': normal_map[x_disaggregated], 'const': 0}
            # TODO: I have a feeling this isn't true...
            assert data['coef'] == normal_map[x_disaggregated]
            data['const'] += x_disaggregated.value

        # TODO: The fact that I had to do this this way makes me think that
        # disaggregated_var_info maybe should have been a map
        for x_disaggregated, x_orig_bigm, x_orig_rBigm in disaggregated_var_info:
            # we add a term for each variable which was disaggregated
            data = expr_data.pop(x_orig_bigm, None)
            if data is not None:
                bigm_cut += data['coef']*(x_orig_bigm - data['const'])
                rBigm_cut += data['coef']*(x_orig_rBigm - data['const'])

        print "Composite cut"
        print "      %s" % bigm_cut

        return({'bigm': bigm_cut, 'rBigM': rBigm_cut})
                
    def constraint_tight(self, model, constraint):
        val = value(constraint.body)
        ans = 0
        #print "    vals:", value(constraint.lower), val, value(constraint.upper)
        if constraint.lower is not None:
            if value(constraint.lower) >= val:
                # tight or in violation of LB
                ans -= 1

        if constraint.upper is not None:
            if value(constraint.upper) <= val:
                # tight or in violation of UB
                ans += 1

        return ans

    def get_linear_constraint_repn(self, cons):
        std_repn = generate_standard_repn(cons.body)
        cons_dict = {}
        cons_dict['lower'] = value(cons.lower)
        cons_dict['upper'] = value(cons.upper)
        cons_dict['body'] = ComponentMap(
            zip(std_repn.linear_vars, std_repn.linear_coefs))
        cons_dict['body'][None] = value(std_repn.constant)

        return cons_dict

    def add_linear_constraints(self, cons1, cons2):
        ans = {'lower': None, 'upper': None, 'body': ComponentMap()}
        all_vars = cons1['body'].items() + \
                   list(ComponentSet(cons2['body'].items()) - \
                        ComponentSet(cons1['body'].items()))
        for (var, coef) in all_vars:
            if var is None:
                ans['body'][None] = cons1['body'][None] + cons2['body'][None]
                continue
            print var.name
            cons2_coef = cons2['body'].get(var)
            cons1_coef = cons1['body'].get(var)
            if cons2_coef is not None and cons1_coef is not None:
                ans['body'][var] = cons1_coef + cons2_coef
            elif cons1_coef is not None:
                ans['body'][var] = cons1_coef
            elif cons2_coef is not None:
                ans['body'][var] = cons2_coef

        bounds_good = False
        cons1_lower = cons1['lower']
        cons2_lower = cons2['lower']
        if cons1_lower is not None and cons2_lower is not None:
            ans['lower'] = cons1_lower + cons2_lower
            bounds_good = True

        cons1_upper = cons1['upper']
        cons2_upper = cons2['upper']
        if cons1_upper is not None and cons2_upper is not None:
            ans['upper'] = cons1_upper + cons2_upper
            bounds_good = True

        # in all other cases we don't actually want add these constraints... I
        # mean, what we would actually do is multiply one of them be negative
        # one and then do it... But I guess I want to assume that I already did
        # that because in the context of FME, I already did
        if not bounds_good:
            raise RuntimeError("You were adding a leq and geq constraint, "
                               "which is a thing you haven't implemented")

        return ans

    def scalar_multiply_linear_constraint(self, cons, scalar):
        for var, coef in cons['body'].items():
            cons['body'][var] = coef*scalar

        if scalar >= 0:
            if cons['lower'] is not None:
                cons['lower'] *= scalar
            if cons['upper'] is not None:
                cons['upper'] *= scalar
        else:
            # we have to flip the constraint
            if cons['lower'] is not None:
                tmp_upper = cons['upper']
                cons['upper'] = scalar*cons['lower']
                cons['lower'] = None
                if tmp_upper is not None:
                    cons['lower'] = scalar*tmp_upper

            elif cons['upper'] is not None:
                tmp_upper = cons['upper']
                # we actually know that lower is None
                cons['upper'] = None
                cons['lower'] = scalar*tmp_upper

        return cons

