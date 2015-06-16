import numpy as np, time, h5py, copy, argparse, os, mpi4py.MPI, sys, pipes, shutil, bunch
from enlib import enmap, utils, pmat, fft, config, array_ops, map_equation, nmat, errors
from enlib import log, bench, scan, dmap, coordinates
from enlib.cg import CG
from enlib.source_model import SourceModel
from enact import data, nmat_measure, filedb, todinfo

config.default("map_bits", 32, "Bit-depth to use for maps and TOD")
config.default("downsample", 1, "Factor with which to downsample the TOD")
config.default("map_precon", "bin", "Preconditioner to use for map-making")
config.default("map_eqsys",  "equ", "The coordinate system of the maps. Can be eg. 'hor', 'equ' or 'gal'.")
config.default("map_cg_nmax", 1000, "Max number of CG steps to perform in map-making")
config.default("verbosity", 1, "Verbosity for output. Higher means more verbose. 0 outputs only errors etc. 1 outputs INFO-level and 2 outputs DEBUG-level messages.")
config.default("task_dist", "size", "How to assign scans to each mpi task. Can be 'plain' for myid:n:nproc-type assignment, 'size' for equal-total-size assignment. The optimal would be 'time', for equal total time for each, but that's not implemented currently.")
config.default("gfilter_jon", False, "Whether to enable Jon's ground filter.")
config.default("map_ptsrc_handling", "subadd", "How to handle point sources in the map. Can be 'plain' for no special treatment, 'subadd' to subtract from the TOD and readd in pixel space, and 'sim' to simulate a pointsource-only TOD.")
config.default("map_ptsrc_eqsys", "cel", "Equation system the point source positions are specified in. Default is 'cel'")
config.default("map_format", "fits", "File format to use when writing maps. Can be 'fits', 'fits.gz' or 'hdf'.")

parser = config.ArgumentParser(os.environ["HOME"] + "/.enkirc")
parser.add_argument("filelist")
parser.add_argument("area")
parser.add_argument("odir")
parser.add_argument("prefix",nargs="?")
parser.add_argument("-d", "--dump", type=str, default="1,2,5,10,20,50,100,200,300,400,500,600,800,1000,1200,1500,2000,3000,4000,5000,6000,8000,10000", help="CG map dump steps")
parser.add_argument("--ncomp",      type=int, default=3,  help="Number of stokes parameters")
parser.add_argument("--ndet",       type=int, default=0,  help="Max number of detectors")
parser.add_argument("--imap",       type=str,             help="Reproject this map instead of using the real TOD data. Format eqsys:filename")
parser.add_argument("--imap-op",    type=str, default='sim', help="What operation to do with imap. Can be 'sim' or 'sub'")
parser.add_argument("--azmap",      type=str,             help="Solve for azimuth signal in addition to map. Example 300:phase:shared, 100:azimuth:individual")
parser.add_argument("--dump-config", action="store_true", help="Dump the configuration file to standard output.")
args = parser.parse_args()

if args.dump_config:
	print config.to_str()
	sys.exit(0)

precon= config.get("map_precon")
dtype = np.float32 if config.get("map_bits") == 32 else np.float64
comm  = mpi4py.MPI.COMM_WORLD
myid  = comm.rank
nproc = comm.size
nmax  = config.get("map_cg_nmax")
ext   = config.get("map_format")
mapsys= config.get("map_eqsys")
tshape= (240,240)

filedb.init()
db = filedb.data
filelist = todinfo.get_tods(args.filelist, filedb.scans)

utils.mkdir(args.odir)
root = args.odir + "/" + (args.prefix + "_" if args.prefix else "")

# Dump our settings
if myid == 0:
	config.save(root + "config.txt")
	with open(root + "args.txt","w") as f:
		f.write(" ".join([pipes.quote(a) for a in sys.argv[1:]]) + "\n")
	with open(root + "env.txt","w") as f:
		for k,v in os.environ.items():
			f.write("%s: %s\n" %(k,v))
	with open(root + "ids.txt","w") as f:
		for id in filelist:
			f.write("%s\n" % id)
	shutil.copyfile(filedb.cjoin(["root","dataset","filedb"]),  root + "filedb.txt")
	try: shutil.copyfile(filedb.cjoin(["root","dataset","todinfo"]), root + "todinfo.txt")
	except IOError: pass
# Set up logging
utils.mkdir(root + "log")
logfile   = root + "log/log%03d.txt" % myid
log_level = log.verbosity2level(config.get("verbosity"))
L = log.init(level=log_level, file=logfile, rank=myid)
# And benchmarking
utils.mkdir(root + "bench")
benchfile = root + "bench/bench%03d.txt" % myid

# Read in all our scans
L.info("Reading %d scans" % len(filelist))
tmpinds    = np.arange(len(filelist))[myid::nproc]
myscans, myinds, mysubs  = [], [], []
for ind in tmpinds:
	try:
		d = scan.read_scan(filelist[ind])
	except IOError:
		try:
			d = data.ACTScan(db[filelist[ind]])
		except errors.DataMissing as e:
			L.debug("Skipped %s (%s)" % (filelist[ind], e.message))
			continue
	d = d[:,::config.get("downsample")]
	if args.ndet > 0: d = d[:args.ndet]
	# Add a subset tag to 
	myscans.append(d)
	myinds.append(ind)
	mysubs.append(0)
	L.debug("Read %s" % filelist[ind])

# Collect scan info
read_ids  = [filelist[ind] for ind in utils.allgatherv(myinds, comm)]
read_ntot = len(read_ids)
L.info("Found %d tods" % read_ntot)
if read_ntot == 0:
	L.info("Giving up")
	sys.exit(1)
read_ndets= utils.allgatherv([len(scan.dets) for scan in myscans], comm)
read_dets = utils.uncat(utils.allgatherv(np.concatenate([scan.dets for scan in myscans]),comm), read_ndets)
# Save accept list
if myid == 0:
	with open(root + "accept.txt", "w") as f:
		for id, dets in zip(read_ids, read_dets):
			f.write("%s %3d: " % (id, len(dets)) + " ".join([str(d) for d in dets]) + "\n")

# Try to get about the same amount of data for each mpi task,
# all at roughly the same part of the sky.
mycosts = [s.nsamp*s.ndet for s in myscans]
myboxes = [
		utils.bounding_box(utils.rewind([
			coordinates.transform(s.sys,mapsys,b[1:,None],time=s.mjd0+b[0,None]/3600/24,site=s.site)[::-1,0] for b in utils.box2corners(s.box)
	],ref="auto")) for s in myscans]
all_costs = comm.allreduce(mycosts)
all_boxes = np.array(comm.allreduce(myboxes))
# Avoid angle wraps.
all_boxes[...,1] = np.sort(utils.rewind(all_boxes[...,1], ref="auto"),-1)
all_inds  = comm.allreduce(myinds)
myinds_old = myinds
# Split into nearby scans
mygroups = dmap.split_boxes_rimwise(all_boxes, all_costs, nproc)[myid]
myinds = [all_inds[i] for group in mygroups for i in group]
mysubs = [gi for gi, group in enumerate(mygroups) for i in group]
mybbox = [utils.bounding_box([all_boxes[i] for i in group]) for group in mygroups]
# And reread the correct files this time. Ideally we would
# transfer this with an mpi all-to-all, but then we would
# need to serialize and unserialize lots of data, which
# would require lots of code.
L.info("Rereading shuffled scans")
myscans = []
for ind in myinds:
	d = data.ACTScan(db[filelist[ind]])[:,::config.get("downsample")]
	if args.ndet > 0: d = d[:args.ndet]
	myscans.append(d)
	L.debug("Read %s" % filelist[ind])

# We now have enough information to set up distributed maps
area = dmap.read_map(args.area, bbox=mybbox, tshape=tshape, comm=comm)
area = dmap.Dmap((args.ncomp,)+area.shape[-2:], area.wcs, area.bbpix, tshape=tshape, comm=comm, dtype=dtype)

# Optinal input map to use in place of signal
imap = None
if args.imap:
	toks = args.imap.split(":")
	imap_sys, fname = ":".join(toks[:-1]), toks[-1]
	if args.imap_op == "sim": tmul, mmul = 0, 1
	elif args.imap_op == "sub": tmul, mmul = 1, -1
	imap = bunch.Bunch(sys=imap_sys or None, map=dmap.read_map(fname, area.bbpix, tshape=tshape, comm=comm).astype(dtype), tmul=tmul, mmul=mmul)

# Optional point source model to subtract
isrc = None
ptsrc_handling = config.get("map_ptsrc_handling")
if ptsrc_handling != 'none':
	isrc_sys = config.get("map_ptsrc_eqsys")
	# Only static point sources supported for now
	fname = db.static.pointsrcs
	if ptsrc_handling == "sim": tmul, pmul = 0,1
	elif ptsrc_handling == "subadd": tmul, pmul = 1,-1
	else: raise ValueError("Unrecognized map_ptsrc_handling: %s" % src_handling)
	model = SourceModel(fname)
	srcmap = area.copy()
	for tile in srcmap.tiles:
		tile[:] = model.draw(tile.shape, tile.wcs, window=True, pad=True)
	isrc = bunch.Bunch(sys=isrc_sys or None, model=model, map=srcmap, tmul=tmul, pmul=pmul)

# Optional azimuth filter
azfilter = None
if config.get("gfilter_jon"):
	azfilter = bunch.Bunch(naz=None, nt=None)

# Optionally solve for azimuth profile
azmap = None
if args.azmap:
	npix, mode, shared = args.azmap.split(":")
	azmap = bunch.Bunch(npix=int(npix), mode=mode, shared=shared=="shared")

L.info("Building equation system")
eq  = map_equation.LinearSystemMap(myscans, area, precon=precon, imap=imap, isrc=isrc, azmap=azmap, azfilter=azfilter, subinds=mysubs)
eq.write(root, ext=ext)
bench.stats.write(benchfile)

L.info("Computing approximate map")
x  = eq.M(eq.b)
bmap = eq.dof.unzip(x)[0]
dmap.write_map(root + "bin." + ext, bmap)

def solve_cg(eq, nmax=1000, ofmt=None, dump=None, azfmt=None):
	cg = CG(eq.A, eq.b, M=eq.M, dot=eq.dof.dot)
	while cg.i < nmax:
		with bench.mark("cg_step"):
			cg.step()
		dt = bench.stats["cg_step"]["time"].last
		L.info("CG step %5d %15.7e %6.1f %6.3f" % (cg.i, cg.err, dt, dt/max(1,len(eq.scans))))
		if ofmt and (cg.i in dump or cg.i % dump[-1] == 0):
			map = eq.dof.unzip(cg.x)[0]
			# Add back sources if necessary. We recompute the map each time because
			# dumps won't happen that frequently, and storing the map takes space.
			# Also finish the azimuth filtering. This is horribly ugly and needs
			# to be redesigned. I should not have to access things deep down like this.
			map = eq.mapeq.postprocess(map, eq.precon.div_map)
			dmap.write_map(ofmt % cg.i, map)
		if azfmt and azmap and azmap.shared and (cg.i in dump or cg.i % dump[-1] == 0) and myid == 0:
			az = eq.dof.unzip(cg.x)[-1]
			np.savetxt(azfmt % cg.i, az[0].T, "%15.7e")
		# Output benchmarking information
		bench.stats.write(benchfile)
	return cg.x

if nmax > 0:
	L.info("Solving equation")
	dump_steps = [int(w) for w in args.dump.split(",")]
	x = solve_cg(eq, nmax=nmax, ofmt=root + "map%04d." + ext, dump=dump_steps, azfmt=root + "az%04d." + ext)
