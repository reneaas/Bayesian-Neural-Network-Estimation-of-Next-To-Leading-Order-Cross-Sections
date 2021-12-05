import functools as ft
import tensorflow as tf
import tensorflow_probability as tfp
from bnn import build_net, chunks, target_log_prob_fn_factory


def pre_train_nn(X_train, y_train, nodes_per_layer, epochs=100):
    """Pre-train NN to get good starting point for HMC.
    Args:
        nodes_per_layer (list): the number of nodes in each dense layer
        X_train (Tensor or np.array): training samples
        y_train (Tensor or np.array): training labels
    Returns:
        Tensor: list of arrays specifying the weights of the trained network
        model: Keras Sequential model
    """
    layers = [tf.keras.layers.Dense(n, activation="relu") for n in nodes_per_layer]
    layers[-1].activation = tf.identity  # Make last layer linear.
    model = tf.keras.Sequential(layers)
    model.compile(loss="mse", optimizer="adam")
    model.fit(X_train, y_train, epochs=epochs, verbose=0)
    return model.get_weights(), model


def trace_fn(current_state, kernel_results, summary_freq=10, callbacks=[]):
    """Can be passed to the HMC kernel to obtain a trace of intermediate
    kernel results and histograms of the network parameters in Tensorboard.
    """
    step = kernel_results.step
    with tf.summary.record_if(tf.equal(step % summary_freq, 0)):
        for idx, tensor in enumerate(current_state):
            count = idx // 2 + 1
            name = ("w" if idx % 2 == 0 else "b") + str(count)
            tf.summary.histogram(name, tensor, step=step)
        return kernel_results, [cb(*current_state) for cb in callbacks]


@tf.function(experimental_compile=True)
def sample_chain(*args, **kwargs):
    """Compile static graph for tfp.mcmc.sample_chain to improve performance."""
    return tfp.mcmc.sample_chain(*args, **kwargs)


def run_hmc(
    target_log_prob_fn,
    step_size=0.01,
    num_leapfrog_steps=10,
    num_burnin_steps=1000,
    num_results=1000,
    current_state=None,
    resume=None,
    log_dir="logs/hmc/",
    sampler="hmc",
    step_size_adapter="simple",
    **kwargs,
):
    """Use adaptive HMC to generate a Markov chain of length num_results.
    Args:
        target_log_prob_fn {callable}: Determines the stationary distribution
        the Markov chain should converge to.
    Returns:
        burnin(s): Discarded samples generated during warm-up
        chain(s): Markov chain(s) of samples distributed according to
            target_log_prob_fn (if converged)
        trace: the data collected by trace_fn
        final_kernel_result: kernel results of the last step (in case the
            computation needs to be resumed)
    """
    err = "Either current_state or resume is required when calling run_hmc"
    assert current_state is not None or resume is not None, err
    summary_writer = tf.summary.create_file_writer(log_dir)
    step_size_adapter = tfp.mcmc.SimpleStepSizeAdaptation
    kernel = tfp.mcmc.HamiltonianMonteCarlo(
        target_log_prob_fn=target_log_prob_fn,
        step_size=step_size,
        num_leapfrog_steps=num_leapfrog_steps
    )
    adaptive_kernel = step_size_adapter(
            kernel, num_adaptation_steps=num_burnin_steps
        )

    if resume:
        prev_chain, prev_trace, prev_kernel_results = resume
        step = len(prev_chain)
        current_state = tf.nest.map_structure(lambda chain: chain[-1], prev_chain)
    else:
        prev_kernel_results = adaptive_kernel.bootstrap_results(current_state)
        step = 0
    
    tf.summary.trace_on(graph=True, profiler=False)
    chain, trace, final_kernel_results = sample_chain(
        kernel=adaptive_kernel,
        current_state=current_state,
        previous_kernel_results=prev_kernel_results,
        num_results=num_burnin_steps + num_results,
        trace_fn=ft.partial(trace_fn, summary_freq=20),
        return_final_kernel_results=True,
        **kwargs,
    )

    with summary_writer.as_default():
        tf.summary.trace_export(name="hmc_trace", step=step)
        
    summary_writer.close()

    if resume:
        chain = nest_concat(prev_chain, chain)
        trace = nest_concat(prev_trace, trace)
    
    burnin, samples = zip(*[(t[:-num_results], t[-num_results:]) for t in chain])
    return burnin, samples, trace, final_kernel_results


def predict_from_chain(chain, X_test, uncertainty="aleatoric+epistemic"):
    """Takes a Markov chain of NN configurations and does the actual
    prediction on a test set X_test including aleatoric and optionally
    epistemic uncertainty estimation.
    """
    err = f"unrecognized uncertainty type: {uncertainty}"
    assert uncertainty in ["aleatoric", "aleatoric+epistemic"], err
    if uncertainty == "aleatoric":
        post_params = [tf.reduce_mean(t, axis=0) for t in chain]
        post_model = build_net(post_params)
        y_pred, y_var = post_model(X_test, training=False)
        return y_pred.numpy(), y_var.numpy()
    if uncertainty == "aleatoric+epistemic":
        restructured_chain = [
            [tensor[i] for tensor in chain] for i in range(len(chain[0]))
        ]

        def predict(params):
            post_model = build_net(params)
            y_pred, y_var = post_model(X_test, training=False)
            return y_pred, y_var

        preds = [predict(chunks(params, 2)) for params in restructured_chain]
        y_pred_mc_samples, y_var_mc_samples = tf.unstack(
            preds,
            axis=1,
        )
        y_pred, y_var_epist = tf.nn.moments(y_pred_mc_samples, axes=0)
        y_var_aleat = tf.reduce_mean(y_var_mc_samples, axis=0)
        y_var_tot = y_var_epist + y_var_aleat
        return y_pred, y_var_tot


def nest_concat(*args, axis=0):
    """Utility function for concatenating a new Markov chain or trace with
    older ones when resuming a previous calculation.
    """
    return tf.nest.map_structure(lambda *parts: tf.concat(parts, axis=axis), *args)
