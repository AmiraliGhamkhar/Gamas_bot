/**
 * Topic emblems from the booklet asset set (`assets/booklet/icons/`, the same
 * illustrations the covers use). Empty states show a real piece of the brand
 * instead of the generic circle-badge-icon cliché.
 */
import bone from "../assets/emblems/bone.png";
import book from "../assets/emblems/book.png";
import capsule from "../assets/emblems/capsule.png";
import chart from "../assets/emblems/chart.png";
import cross from "../assets/emblems/cross.png";
import flask from "../assets/emblems/flask.png";
import heart from "../assets/emblems/heart.png";
import leaf from "../assets/emblems/leaf.png";
import network from "../assets/emblems/network.png";

export type EmblemName =
  | "bone"
  | "book"
  | "capsule"
  | "chart"
  | "cross"
  | "flask"
  | "heart"
  | "leaf"
  | "network";

const EMBLEMS: Record<EmblemName, string> = {
  bone,
  book,
  capsule,
  chart,
  cross,
  flask,
  heart,
  leaf,
  network,
};

export function Emblem({
  name,
  size = 48,
}: {
  name: EmblemName;
  size?: number;
}) {
  return (
    <img
      src={EMBLEMS[name]}
      alt=""
      aria-hidden="true"
      width={size}
      height={size}
      className="h-auto w-auto select-none"
      style={{ width: size, height: size }}
    />
  );
}
