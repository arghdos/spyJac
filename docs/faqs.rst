Frequently asked Questions
##########################

=============
Common Issues
=============


Static TLS errors
-----------------


I get an error stating::

    OSError: dlopen: cannot load any more object with static TLS

when running validation testing.


This has been known to occur in SLURM or related job-batch systems when loading the Adept library for auto-differentiation execution. To work around this, you should set the `environment variable LD_PRELOAD`_, e.g.::

    LD_PRELOAD=/path/to/adept/install/lib/libadept.so python -m pyjac.functional_tester ...


.. _environment variable LD_PRELOAD: https://stackoverflow.com/a/45640803/1667311

I get an error complaining that the function `timersub` doesn't exist!
----------------------------------------------------------------------

To fix this, add the following to your ``siteconf.py``::

    CC_FLAGS = ['-D_BSD_SOURCE -D_SVID_SOURCE -D_POSIX_C_SOURCE=200809']
