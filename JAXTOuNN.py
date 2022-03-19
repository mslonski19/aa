
# We begin by importing the necessary libraries
import numpy as np

import jax
import jax.numpy as jnp
from jax import jit, grad, random, jacfwd, value_and_grad
from jax.ops import index, index_add, index_update
from jax.experimental import stax, optimizers
from functools import partial
import jax.nn as nn
import matplotlib
import matplotlib.pyplot as plt
from matplotlib import colors
from examples import getExampleBC
from Mesher import RectangularGridMesher
from projections import computeFourierMap, applyFourierMap, applySymmetry
from network import TopNet
from FE_Solver import JAXSolver
#%%
ndim, nelx, nely = 2, 40, 20
elemSize = np.array([1., 1.])

exampleName, bcSettings, symMap = getExampleBC(1, nelx, nely);

mesh = RectangularGridMesher(ndim, nelx, nely, elemSize, bcSettings);


# observe that xyElems is an array from jax. 
# This makes tracking the variable possible
xyElems = jnp.array(mesh.generatePoints())
xyElems = applySymmetry(xyElems, symMap)
print(xyElems.shape)

"""### Material"""

#Next we define the relevant material property. 
# We are concerned only with structural mech
# at the moment. penal here refers to the SIMP penalization constant
material = {'Emax':1., 'Emin':1e-3, 'nu':0.3, 'penal':1.}

# with the material defined, we can now calculate the base constitutive matrix
def getD0(material):
  # the base constitutive matrix assumes unit 
  #area element with E = 1. and nu prescribed.
  # the material is also assumed to be isotropic.
  # returns a matrix of size (8X8)
  E = 1.
  nu = material['nu'];
  k = np.array([1/2-nu/6,1/8+nu/8,-1/4-nu/12,-1/8+3*nu/8,\
                  -1/4+nu/12,-1/8-nu/8,nu/6,1/8-3*nu/8])
  KE = \
  E/(1-nu**2)*np.array([ [k[0], k[1], k[2], k[3], k[4], k[5], k[6], k[7]],
  [k[1], k[0], k[7], k[6], k[5], k[4], k[3], k[2]],
  [k[2], k[7], k[0], k[5], k[6], k[3], k[4], k[1]],
  [k[3], k[6], k[5], k[0], k[7], k[2], k[1], k[4]],
  [k[4], k[5], k[6], k[7], k[0], k[1], k[2], k[3]],
  [k[5], k[4], k[3], k[2], k[1], k[0], k[7], k[6]],
  [k[6], k[3], k[4], k[1], k[2], k[7], k[0], k[5]],
  [k[7], k[2], k[1], k[4], k[3], k[6], k[5], k[0]] ])
  return KE

material['D0'] = getD0(material)



nnSettings = {'outputDim':1, 'numNeuronsPerLayer':20,  'numLayers':2}





# Optimization params
lossMethod = {'type':'penalty', 'alpha0':0.05, 'delAlpha':0.05}
#lossMethod = {'type':'logBarrier', 't0':3, 'mu':1.1};

fourierMap = {'isOn': True, 'minRadius':4., \
              'maxRadius':80., 'numTerms':  200}

fourierMap['map'] = computeFourierMap(mesh, fourierMap)


optimizationParams = {'maxEpochs':100, 'learningRate':0.01, 'desiredVolumeFraction':0.5,\
                     'lossMethod':lossMethod}

def optimizeDesign(xy, optParams, mesh, material, fourierMap):
  FE = JAXSolver(mesh, material)
  # input projection
  if(fourierMap['isOn']):
    xy = applyFourierMap(xy, fourierMap)
    nnSettings['inputDim'] = 2*fourierMap['numTerms']
  else:
    nnSettings['inputDim'] = FE.mesh.ndim
  
  
 
  # make the NN
  topNet = TopNet(nnSettings)

  # optimizer
  opt_init, opt_update, get_params = optimizers.adam(optParams['learningRate'])
  opt_state = opt_init(topNet.wts)
  opt_update = jit(opt_update)
  
  # fwd once to get J0-scaling param
  density0  = topNet.forward(get_params(opt_state), xy)
  J0 = FE.objectiveHandle(density0.reshape(-1))

  def getYoungsModulus(density):
    material['penal'] = min(8., 1. + epoch*0.02)
    Y = material['Emin'] + \
          (material['Emax']-material['Emin'])*(density+0.001)**material['penal']
    return Y
  #-----------------------#
  # loss function
  def computeLoss(nnwts):
    density  = 0.01 + topNet.forward(nnwts, xy)
    Y = getYoungsModulus(density)
    volcons = (jnp.mean(density)/optParams['desiredVolumeFraction'])- 1.
    J = FE.objectiveHandle(Y.reshape(-1))

    if(optParams['lossMethod']['type'] == 'penalty'):
      alpha = optParams['lossMethod']['alpha0'] + \
              epoch*optParams['lossMethod']['delAlpha'] # penalty method
      loss = J/J0 + alpha*volcons**2;
    if(optParams['lossMethod']['type'] == 'logBarrier'):
      t = optParams['lossMethod']['t0']* \
                        optParams['lossMethod']['mu']**epoch
      if(volcons < (-1/t**2)):
        psi = -jnp.log(-volcons)/t
      else:
        psi = t*volcons - jnp.log(1/t**2)/t + 1/t
      loss = J/J0 + psi

    return loss;
  
  # optimization loop
  for epoch in range(optParams['maxEpochs']):
    opt_state = opt_update(epoch, \
                optimizers.clip_grads(jax.grad(computeLoss)(get_params(opt_state)), 1.),\
                opt_state)

    if(epoch%10 == 0):
      density = topNet.forward(get_params(opt_state), xy)
      Y = getYoungsModulus(density)
      J = FE.objectiveHandle(Y.reshape(-1))
      volf= jnp.mean(density)
      if(epoch == 10):
        J0 = J;
      status = 'epoch {:d}, J {:.2E}, vf {:.2F}'.format(epoch, J/J0, volf);
      print(status)
      if(epoch%30 == 0):
        FE.mesh.plotFieldOnMesh(density, status)
  return topNet, get_params(opt_state)

"""# Run"""

network, nnwts = optimizeDesign(xyElems, optimizationParams, mesh, material, fourierMap)