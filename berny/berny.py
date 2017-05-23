# This Source Code Form is subject to the terms of the Mozilla Public
# License, v. 2.0. If a copy of the MPL was not distributed with this
# file, You can obtain one at http://mozilla.org/MPL/2.0/.
from __future__ import print_function
from collections import namedtuple
import numpy as np
from itertools import chain
from numpy import dot, eye
from numpy.linalg import norm
import json

from . import Math
from .geomlib import InternalCoords


defaults = {
    'gradientmax': 0.45e-3,
    'gradientrms': 0.3e-3,
    'stepmax': 1.8e-3,
    'steprms': 1.2e-3,
    'maxsteps': 100,
    'trust': 0.3,
    'debug': None
}


def info(*args, **kwargs):
    print(*args, **kwargs)


def Berny(geom, params=None, log=print):
    params = dict(chain(defaults.items(), (params or {}).items()))
    nsteps = 0
    trust = params['trust']
    coords = InternalCoords(geom)
    hessian = coords.hessian_guess(geom)
    weights = coords.weights(geom)
    debug = []
    for line in str(coords).split('\n'):
        log(line)
    best, previous, predicted, interpolated = None, None, None, None
    while True:
        energy, gradients = yield geom
        yield
        gradients = np.array(gradients)
        nsteps += 1
        if nsteps > params['maxsteps']:
            break
        log('Energy: {:.12}'.format(energy))
        B = coords.B_matrix(geom)
        B_inv = Math.ginv(B)
        current = PESPoint(
            coords.eval_geom(geom),
            energy,
            dot(B_inv.T, gradients.reshape(-1))
        )
        if nsteps > 1:
            update_hessian(hessian, current.q-best.q, current.g-best.g)
            trust = update_trust(
                trust,
                current.E-previous.E,
                predicted.E-interpolated.E,
                predicted.q-interpolated.q
            )
            dq = best.q-current.q
            t, E = linear_search(
                current.E, best.E, dot(current.g, dq), dot(best.g, dq)
            )
            interpolated = PESPoint(current.q+t*dq, E, t*best.g+(1-t)*current.g)
        else:
            interpolated = current
        proj = dot(B, B_inv)
        hessian_proj = \
            proj.dot(hessian).dot(proj) + 1000*(eye(len(coords))-proj)
        dq, dE, on_sphere = quadratic_step(
            dot(proj, interpolated.g), hessian_proj, weights, trust
        )
        predicted = PESPoint(interpolated.q+dq, interpolated.E+dE, None)
        dq = predicted.q-current.q
        log('Total step: RMS: {:.3}, max: {:.3}'.format(Math.rms(dq), max(abs(dq))))
        geom = geom.copy()
        q = coords.update_geom(geom, current.q, predicted.q-current.q, B_inv)
        future = PESPoint(q, None, None)
        if params['debug']:
            debug.append({
                'nstep': nsteps,
                'trust': trust,
                'hessian': hessian.copy(),
                'gradients': gradients,
                'coords': geom.coords,
                'energy': energy,
                'q': current.q,
                'dq': dq
            })
            with open(params['debug'], 'w') as f:
                json.dump(debug, f, indent=4, cls=ArrayEncoder)
        if converged(gradients, future.q-current.q, on_sphere, params):
            break
        previous = current
        if nsteps == 1 or current.E < best.E:
            best = current


PESPoint = namedtuple('PESPoint', 'q E g')


def update_hessian(H, dq, dg):
    dH = dg[None, :]*dg[:, None]/dot(dq, dg) - \
        H.dot(dq[None, :]*dq[:, None]).dot(H)/dq.dot(H).dot(dq)  # BFGS update
    info('Hessian update information:')
    info('* Change: RMS: {:.3}, max: {:.3}'.format(Math.rms(dH), abs(dH).max()))
    H[:, :] = H+dH


def update_trust(trust, dE, dE_predicted, dq):
    if dE != 0:
        r = dE/dE_predicted  # Fletcher's parameter
    else:
        r = 1.
    info("Trust update: Fletcher's parameter: {:.3}".format(r))
    if r < 0.25:
        return norm(dq)/4
    elif r > 0.75 and abs(norm(dq)-trust) < 1e-10:
        return 2*trust
    else:
        return trust


def linear_search(E0, E1, g0, g1):
    info('Linear interpolation:')
    info('* Energies: {:.8}, {:.8}'.format(E0, E1))
    info('* Derivatives: {:.3}, {:.3}'.format(g0, g1))
    t, E = Math.fit_quartic(E0, E1, g0, g1)
    if t is None or t < -1 or t > 2:
        t, E = Math.fit_cubic(E0, E1, g0, g1)
        if t is None or t < 0 or t > 1:
            if E0 <= E1:
                info('* No fit succeeded, staying in new point')
                return 0, E0

            else:
                info('* No fit succeeded, returning to best point')
                return 1, E1
        else:
            msg = 'Cubic interpolation was performed'
    else:
        msg = 'Quartic interpolation was performed'
    info('* {}: t = {:.3}'.format(msg, t))
    info('* Interpolated energy: {:.8}'.format(E))
    return t, E


def quadratic_step(g, H, w, trust):
    ev = np.linalg.eigvalsh((H+H.T)/2)
    rfo = np.vstack((np.hstack((H, g[:, None])),
                     np.hstack((g, 0))[None, :]))
    D, V = np.linalg.eigh((rfo+rfo.T)/2)
    dq = V[:-1, 0]/V[-1, 0]
    l = D[0]
    if norm(dq) <= trust:
        info('Pure RFO step was performed:')
        on_sphere = False
    else:
        def steplength(l):
            return norm(np.linalg.solve(l*eye(H.shape[0])-H, g))-trust
        l = Math.findroot(steplength, ev[0])  # minimization on sphere
        dq = np.linalg.solve(l*eye(H.shape[0])-H, g)
        on_sphere = False
        info('Minimization on sphere was performed:')
    dE = dot(g, dq)+0.5*dq.dot(H).dot(dq)  # predicted energy change
    info('* Trust radius: {:.2}'.format(trust))
    info('* Number of negative eigenvalues: {}'.format((ev < 0).sum()))
    info('* Lowest eigenvalue: {:.3}'.format(ev[0]))
    info('* lambda: {:.3}'.format(l))
    info('Quadratic step: RMS: {:.3}, max: {:.3}'.format(Math.rms(dq), max(abs(dq))))
    info('* Predicted energy change: {:.3}'.format(dE))
    return dq, dE, on_sphere


def converged(forces, step, on_sphere, params):
    criteria = [
        ('Gradient RMS', Math.rms(forces), params['gradientrms']),
        ('Gradient maximum', np.max(abs(forces)), params['gradientmax'])]
    if on_sphere:
        criteria.append(('Minimization on sphere', False))
    else:
        criteria.extend([
            ('Step RMS', Math.rms(step), params['steprms']),
            ('Step maximum', np.max(abs(step)), params['stepmax'])])
    info('Convergence criteria:')
    all_matched = True
    for crit in criteria:
        if len(crit) > 2:
            result = crit[1] < crit[2]
            msg = '{:.3} {} {:.3}'.format(crit[1], '<' if result else '>', crit[2])
        else:
            result = crit[2]
            msg = None
        msg = '{}: {}'.format(crit[0], msg) if msg else crit[0]
        msg = '* {} => {}'.format(msg, 'OK' if result else 'no')
        info(msg)
        if not result:
            all_matched = False
    if all_matched:
        info('* All criteria matched')
    return all_matched


class ArrayEncoder(json.JSONEncoder):
    def default(self, obj):
        if obj is np.nan:
            return None
        try:
            return obj.tolist()
        except AttributeError:
            return super().default(obj)
