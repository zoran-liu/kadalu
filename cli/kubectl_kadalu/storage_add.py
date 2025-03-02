"""
'storage-add' sub command
"""

# noqa # pylint: disable=duplicate-code
# noqa # pylint: disable=too-many-branches

#To prevent Py2 to interpreting print(val) as a tuple.
from __future__ import print_function

import json
import os
import sys
import tempfile

import utils
from storage_yaml import to_storage_yaml
import storage_add_parser


def set_args(name, subparsers):
    """ add arguments, and their options """
    # TODO: Sub group arguments to relax validation manually
    # https://docs.python.org/3/library/argparse.html#argument-groups
    parser = subparsers.add_parser(name)
    arg = parser.add_argument

    arg("name", help="Storage Name")
    arg("storage_units", help="List of Storage units (Alternate syntax)", nargs="*")
    arg("--storage-unit-type",
        help="Storage Unit Type",
        choices=["path", "pvc", "device"],
        default=None)
    arg("--type",
        help="Storage Type",
        choices=["Replica1", "Replica3", "External", "Replica2", "Disperse"],
        default=None)
    arg("--volume-id",
        help="Volume ID of previously created volume",
        default=None)
    arg("--pv-reclaim-policy",
        help="PV Reclaim Policy",
        choices=["delete", "archive", "retain"],
        default=None)
    arg("--device",
        help=("Storage device in <node>:<device> format, "
              "Example: --device kube1.example.com:/dev/vdc"),
        default=[],
        action="append")
    arg("--path",
        help=("Storage path in <node>:<path> format, "
              "Example: --path kube1.example.com:/exports/data"),
        default=[],
        action="append")
    arg("--pvc",
        help="Storage from pvc, Example: --pvc local-pvc-1",
        default=[],
        action="append")
    arg("--external",
        help=("Storage from external gluster, "
              "Example: --external gluster-node:/gluster-volname"),
        default=None)
    arg("--tiebreaker",
        help=
        ("If type is 'Replica2', one can have a tiebreaker node along "
         "with it. like '--tiebreaker tie-breaker-node-name:/data/tiebreaker'"
         ),
        default=None)
    arg("--gluster-options",
        help=(
            "Can only be used in conjunction with '--external' argument. "
            "Supply options to be used while mounting external gluster cluster"
            "Example: --gluster-options 'log-level=WARNING,"
            "reader-thread-count=2,log-file=/var/log/gluster.log'"
         ),
        default=None)
    arg("--data",
        help="Number of Disperse data Storage units",
        type=int,
        dest="disperse_data",
        default=0)
    arg("--redundancy",
        help="Number of Disperse Redundancy Storage units",
        type=int,
        dest="disperse_redundancy",
        default=0)
    # Default for 'kadalu-format' is set in CRD
    arg("--kadalu-format",
            help=("Specifies whether the  cluster should be provisioned in "
                  "kadalu native (1 PV:1 Subdir) or non-native "
                  "(1 PV: 1 Volume) format. Default: native"),
            choices=["native", "non-native"],
            default=None)
    arg("--single-pv-per-pool",
        help=("Specifies whether the  cluster should be provisioned as "
              "1 PV == 1 Pool. Default: False"),
        action="store_true")
    utils.add_global_flags(parser)


# pylint: disable=too-many-statements
def validate(args):
    """ validate arguments """
    if args.external is not None:
        if args.type and args.type != "External":
            print("'--external' option is used only with '--type External'",
                  file=sys.stderr)
            sys.exit(1)

        if ":" not in args.external:
            print(
                "Invalid external storage details. Please specify "
                "details in the format <node>:/<volname>",
                file=sys.stderr)
            sys.exit(1)

        # Set type to External as '--external' option is provided
        args.type = "External"

    if args.external is None:
        fail = False

        if args.gluster_options:
            print("'--gluster-options' is used only with '--type External'",
                    file=sys.stderr)
            fail = True

        if fail:
            sys.exit(1)

    if args.tiebreaker:
        if args.type != "Replica2":
            print(
                "'--tiebreaker' option should be used only with "
                "type 'Replica2'",
                file=sys.stderr)
            sys.exit(1)
        if ":" not in args.tiebreaker:
            print(
                "Invalid tiebreaker details. Please specify details "
                "in the format <node>:/<path>",
                file=sys.stderr)
            sys.exit(1)

    if not args.type:
        args.type = "Replica1"

    if len(args.storage_units) > 0:
        # Try parsing the Gluster compatible syntax
        tokens = storage_add_parser.tokenizer(args.storage_units)
        req = storage_add_parser.parser(tokens)
        try:
            storage_add_parser.validate(req)
            args.type = storage_add_parser.volume_type(req)
            if args.type != "External" and args.storage_unit_type is None:
                print("--storage-unit-type is not specified")
                sys.exit(1)

            if args.type == "Disperse":
                dist_grp1 = req.distribute_groups[0]
                args.disperse_data = dist_grp1.disperse_count - dist_grp1.redundancy_count
                args.disperse_redundancy = dist_grp1.redundancy_count

            storage_units = storage_add_parser.get_all_storage_units(req)

            if args.storage_unit_type == "device":
                args.device = storage_units
            elif args.storage_unit_type == "path":
                args.path = storage_units
            elif args.storage_unit_type == "pvc":
                args.pvc = storage_units
            elif args.type == "External":
                args.external = storage_units[0]
        except storage_add_parser.InvalidVolumeCreateRequest as ex:
            print(ex)
            sys.exit(1)

    num_storages = (len(args.device) + len(args.path) + len(args.pvc)) or \
                   (1 if args.external is not None else 0)

    if num_storages == 0:
        print("Please specify at least one storage", file=sys.stderr)
        sys.exit(1)

    subvol_size = 1
    if args.type.startswith("Replica"):
        subvol_size = int(args.type.replace("Replica", ""))

    if args.type == "Disperse":
        if args.disperse_data == 0 or args.disperse_redundancy == 0:
            print("Disperse data(`--data`) or redundancy(`--redundancy`) "
                  "are not specified.", file=sys.stderr)
            sys.exit(1)

        subvol_size = args.disperse_data + args.disperse_redundancy

        # redundancy must be greater than 0, and the total number
        # of bricks must be greater than 2 * redundancy. This
        # means that a dispersed volume must have a minimum of 3 bricks.
        if subvol_size <= (2 * args.disperse_redundancy):
            print("Invalid redundancy for the Disperse Storage",
                  file=sys.stderr)
            sys.exit(1)

        # stripe_size = (bricks_count - redundancy) * 512
        # Using combinations of #Bricks/redundancy that give a power
        # of two for the stripe size will make the disperse volume
        # perform better in most workloads because it's more typical
        # to write information in blocks that are multiple of two
        # https://docs.gluster.org/en/latest/Administrator-Guide
        #    /Setting-Up-Volumes/#creating-dispersed-volumes
        if args.disperse_data % 2 != 0:
            print("Disperse Configuration is not Optimal", file=sys.stderr)
            sys.exit(1)

    if num_storages % subvol_size != 0:
        print("Number of storages not matching for type=%s" % args.type,
              file=sys.stderr)
        sys.exit(1)

    kube_nodes = get_kube_nodes(args)

    for dev in args.device:
        if ":" not in dev:
            print(
                "Invalid storage device details. Please specify device "
                "details in the format <node>:<device>",
                file=sys.stderr)
            sys.exit(1)
        if (not args.dry_run) and (dev.split(":")[0] not in kube_nodes):
            print("Node name does not appear to be valid: " + dev)
            sys.exit(1)

    for path in args.path:
        if ":" not in path:
            print(
                "Invalid storage path details. Please specify path "
                "details in the format <node>:<path>",
                file=sys.stderr)
            sys.exit(1)

        if (not args.dry_run) and (path.split(":")[0] not in kube_nodes):
            print("Node name does not appear to be valid: " + path)
            sys.exit(1)


def get_kube_nodes(args):
    """ gets all nodes  """
    if args.dry_run:
        return []

    cmd = utils.kubectl_cmd(args) + ["get", "nodes", "-ojson"]
    try:
        resp = utils.execute(cmd)
        data = json.loads(resp.stdout)
        nodes = []
        for nodedata in data["items"]:
            nodes.append(nodedata["metadata"]["name"])

        print("The following nodes are available:\n  %s" % ", ".join(nodes))
        print()
        return nodes
    except utils.CommandError as err:
        utils.command_error(cmd, err.stderr)
        return None
    except FileNotFoundError:
        utils.kubectl_cmd_help(args.kubectl_cmd)
        return None


def storage_add_data(args):
    """ Build the config file """
    content = {
        "apiVersion": "kadalu-operator.storage/v1alpha1",
        "kind": "KadaluStorage",
        "metadata": {
            "name": args.name
        },
        "spec": {
            "type": args.type,
            "storage": []
        }
    }

    # Pv Reclaim Policy is specified, add to either external or native type
    if args.pv_reclaim_policy:
        content["spec"]["pvReclaimPolicy"] = args.pv_reclaim_policy

    if args.volume_id:
        content["spec"]["volume_id"] = args.volume_id

    if args.single_pv_per_pool:
        content["spec"]["single_pv_per_pool"] = args.single_pv_per_pool

    # External details are specified, no 'storage' section required
    if args.external:
        node, vol = args.external.split(":", 1)
        nodes = node.split(',')
        g_opts = ""
        if args.gluster_options:
            # Options are passed as a single string separated by commas (,) in
            # 'key=value' format and can be used without any changes while
            # mounting external gluster cluster
            g_opts = args.gluster_options
        content["spec"]["details"] = {
            "gluster_hosts": nodes,
            "gluster_volname": vol.strip("/"),
            "gluster_options": g_opts,
        }
        return content

    # Everything below can be provided for a 'Replica3' setup.
    # Or two types of data can be provided for 'Replica2'.
    # So, return only at the end.

    # Device details are specified
    if args.device:
        for devdata in args.device:
            node, dev = devdata.split(":", 1)
            content["spec"]["storage"].append({"node": node, "device": dev})

    # If Path is specified
    if args.path:
        for pathdata in args.path:
            node, path = pathdata.split(":", 1)
            content["spec"]["storage"].append({"node": node, "path": path})

    # If PVC is specified
    if args.pvc:
        for pvc in args.pvc:
            content["spec"]["storage"].append({"pvc": pvc})

    # TODO: Support for different port can be added later
    if args.type == "Replica2" and args.tiebreaker is not None:
        node, path = args.tiebreaker.split(":", 1)
        content["spec"]["tiebreaker"] = {
            "node": node,
            "path": path,
            "port": 24007
        }

    if args.type == "Disperse":
        content["spec"]["disperse"] = {
            "data": args.disperse_data,
            "redundancy": args.disperse_redundancy
        }

    return content


def run(args):
    """ Adds the subcommand arguments back to main CLI tool """
    data = storage_add_data(args)

    yaml_content = to_storage_yaml(data)
    print("Storage Yaml file for your reference:\n")
    print(yaml_content)

    if args.dry_run:
        return

    if not args.script_mode:
        answer = ""
        valid_answers = ["yes", "no", "n", "y"]
        while answer not in valid_answers:
            answer = input("Is this correct?(Yes/No): ")
            answer = answer.strip().lower()

        if answer in ["n", "no"]:
            return

    config, tempfile_path = tempfile.mkstemp(prefix="kadalu")
    try:
        with os.fdopen(config, 'w') as tmp:
            tmp.write(yaml_content)

        cmd = utils.kubectl_cmd(args) + ["apply", "-f", tempfile_path]
        resp = utils.execute(cmd)
        print("Storage add request sent successfully")
        print(resp.stdout)
        print()
    except utils.CommandError as err:
        os.remove(tempfile_path)
        utils.command_error(cmd, err.stderr)
    except FileNotFoundError:
        os.remove(tempfile_path)
        utils.kubectl_cmd_help(args.kubectl_cmd)
    finally:
        if os.path.exists(tempfile_path):
            os.remove(tempfile_path)
